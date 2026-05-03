from __future__ import annotations

import hashlib
import logging
import time

from kubernetes import client, config, stream
from kubernetes.client import V1Container, V1EnvVar, V1ObjectMeta, V1Pod, V1PodSpec
from kubernetes.client.exceptions import ApiException

from .config import Settings
from .messages import clamp_sms_reply

LOGGER = logging.getLogger(__name__)


class ChatPodManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.namespace = settings.kubernetes_namespace
        self.idle_seconds = settings.chat_pod_idle_seconds
        self.timeout_seconds = settings.chat_pod_timeout_seconds
        self._load_kube_config()
        self.core = client.CoreV1Api()

    def ask(self, sender: str, message: str) -> str:
        pod_name = self._pod_name(sender)
        self._ensure_pod(pod_name, sender)
        self._wait_until_running(pod_name)
        self._mark_active(pod_name)
        command = [
            "sms-chatgpt-worker",
            "--message",
            message,
        ]
        LOGGER.info("Executing chat worker in pod %s", pod_name)
        response = stream.stream(
            self.core.connect_get_namespaced_pod_exec,
            pod_name,
            self.namespace,
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _request_timeout=self.timeout_seconds,
        )
        self._mark_active(pod_name)
        return clamp_sms_reply(response)

    def cleanup_idle_pods(self) -> None:
        pods = self.core.list_namespaced_pod(
            namespace=self.namespace,
            label_selector="app=sms-chatgpt,managed-by=sms-chatgpt-daemon",
        )
        now = int(time.time())
        for pod in pods.items:
            last_active = int((pod.metadata.annotations or {}).get("sms-chatgpt/last-active", "0"))
            if last_active and now - last_active > self.idle_seconds:
                LOGGER.info("Deleting idle pod %s", pod.metadata.name)
                self.core.delete_namespaced_pod(pod.metadata.name, self.namespace)

    def _ensure_pod(self, pod_name: str, sender: str) -> None:
        try:
            self.core.read_namespaced_pod(pod_name, self.namespace)
            return
        except ApiException as exc:
            if exc.status != 404:
                raise

        pod = V1Pod(
            metadata=V1ObjectMeta(
                name=pod_name,
                labels={
                    "app": "sms-chatgpt",
                    "managed-by": "sms-chatgpt-daemon",
                },
                annotations={
                    "sms-chatgpt/sender-hash": self._sender_hash(sender),
                    "sms-chatgpt/last-active": str(int(time.time())),
                },
            ),
            spec=V1PodSpec(
                restart_policy="Never",
                containers=[
                    V1Container(
                        name="chat",
                        image=self.settings.chat_pod_image,
                        command=["sleep", "3600"],
                        env=[
                            V1EnvVar(name="LLM_PROVIDER", value=self.settings.llm_provider),
                            V1EnvVar(name="OPENAI_API_KEY", value=self.settings.openai_api_key or ""),
                            V1EnvVar(name="OPENAI_MODEL", value=self.settings.openai_model),
                        ],
                    )
                ],
            ),
        )
        LOGGER.info("Creating chat pod %s", pod_name)
        self.core.create_namespaced_pod(self.namespace, pod)

    def _wait_until_running(self, pod_name: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            pod = self.core.read_namespaced_pod(pod_name, self.namespace)
            if pod.status.phase == "Running":
                return
            if pod.status.phase in {"Failed", "Succeeded"}:
                raise RuntimeError(f"Pod {pod_name} ended before it could handle chat")
            time.sleep(1)
        raise TimeoutError(f"Timed out waiting for pod {pod_name} to run")

    def _mark_active(self, pod_name: str) -> None:
        self.core.patch_namespaced_pod(
            name=pod_name,
            namespace=self.namespace,
            body={"metadata": {"annotations": {"sms-chatgpt/last-active": str(int(time.time()))}}},
        )

    def _pod_name(self, sender: str) -> str:
        return f"sms-chat-{self._sender_hash(sender)[:16]}"

    @staticmethod
    def _sender_hash(sender: str) -> str:
        return hashlib.sha256(sender.encode("utf-8")).hexdigest()

    @staticmethod
    def _load_kube_config() -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
