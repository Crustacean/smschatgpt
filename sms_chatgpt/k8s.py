from __future__ import annotations

import hashlib
import json
import logging
import time

try:
    from kubernetes import client, config, stream
    from kubernetes.client import V1Container, V1EnvVar, V1ObjectMeta, V1Pod, V1PodSpec
    from kubernetes.client.exceptions import ApiException
except ModuleNotFoundError:
    client = config = stream = None
    V1Container = V1EnvVar = V1ObjectMeta = V1Pod = V1PodSpec = None

    class ApiException(Exception):
        status = None

from .config import Settings
from .messages import clamp_sms_reply
from .poll_manager import OutboundSms, PollResponse
from .polls import ACTIVE, PENDING, contains_poll_intent, hash_msisdn, parse_creator_command

LOGGER = logging.getLogger(__name__)


class ChatPodManager:
    worker_command = ["python", "-m", "sms_chatgpt.worker"]

    def __init__(self, settings: Settings) -> None:
        if client is None or config is None or stream is None:
            raise RuntimeError("The kubernetes package is required when SESSION_BACKEND=kubernetes")
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
        command = [*self.worker_command, "--message", message]
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
                            V1EnvVar(name="CHAT_HISTORY_FILE", value=self.settings.chat_history_file),
                            V1EnvVar(name="CHAT_HISTORY_MAX_TURNS", value=str(self.settings.chat_history_max_turns)),
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


class PollPodManager:
    worker_command = ["python", "-m", "sms_chatgpt.poll_worker"]
    creator_annotation = "sms-chatgpt/creator-msisdn"

    def __init__(self, settings: Settings) -> None:
        if client is None or config is None or stream is None:
            raise RuntimeError("The kubernetes package is required when SESSION_BACKEND=kubernetes")
        self.settings = settings
        self.namespace = settings.kubernetes_namespace
        self.timeout_seconds = settings.chat_pod_timeout_seconds
        self._load_kube_config()
        self.core = client.CoreV1Api()

    def handle_message(self, sender: str, body: str) -> PollResponse:
        sender_hash = hash_msisdn(sender, self.settings.poll_hash_salt)
        status = self._status()
        state = status.get("state")

        if not status.get("exists"):
            if not contains_poll_intent(body, self.settings.poll_keywords):
                return PollResponse(False)
            self._ensure_pod(sender)
            result = self._exec("draft", "--creator-hash", sender_hash, "--message", body)
            return PollResponse(True, result.get("reply"))

        if state and state.get("status") == PENDING:
            if sender_hash != state.get("creator_hash"):
                if contains_poll_intent(body, self.settings.poll_keywords):
                    return PollResponse(True, "A poll is already pending.")
                return PollResponse(False)
            command, _details = parse_creator_command(body)
            if command == "confirm":
                result = self._exec("confirm")
            elif command == "cancel":
                result = self._exec("cancel")
                self._delete_pod()
            else:
                result = self._exec("amend", "--message", body)
            return PollResponse(bool(result.get("handled", True)), result.get("reply"))

        if state and state.get("status") == ACTIVE:
            result = self._exec("vote", "--voter-hash", sender_hash, "--message", body)
            if result.get("route_to_chat"):
                if contains_poll_intent(body, self.settings.poll_keywords):
                    return PollResponse(True, "A poll is already active.")
                return PollResponse(False)
            return PollResponse(bool(result.get("handled", True)), result.get("reply"))

        return PollResponse(False)

    def close_expired(self) -> list[OutboundSms]:
        status = self._status()
        if not status.get("exists") or not status.get("expired"):
            return []
        creator = self._creator_phone()
        result = self._exec("finalize")
        self._delete_pod()
        if not creator or not result.get("reply"):
            return []
        return [OutboundSms(creator, clamp_sms_reply(result["reply"]))]

    def _ensure_pod(self, creator_phone: str) -> None:
        try:
            existing = self.core.read_namespaced_pod(self.settings.poll_pod_name, self.namespace)
            if existing.metadata.deletion_timestamp or existing.status.phase in {"Failed", "Succeeded"}:
                self._delete_pod()
                self._wait_until_deleted()
            else:
                return
        except ApiException as exc:
            if exc.status != 404:
                raise

        pod = V1Pod(
            metadata=V1ObjectMeta(
                name=self.settings.poll_pod_name,
                labels={
                    "app": "sms-chatgpt-poll",
                    "managed-by": "sms-chatgpt-daemon",
                },
                annotations={
                    self.creator_annotation: creator_phone,
                    "sms-chatgpt/last-active": str(int(time.time())),
                },
            ),
            spec=V1PodSpec(
                restart_policy="Never",
                containers=[
                    V1Container(
                        name="poll",
                        image=self.settings.chat_pod_image,
                        command=["sleep", "3600"],
                        env=[
                            V1EnvVar(name="LLM_PROVIDER", value=self.settings.llm_provider),
                            V1EnvVar(name="OPENAI_API_KEY", value=self.settings.openai_api_key or ""),
                            V1EnvVar(name="OPENAI_MODEL", value=self.settings.openai_model),
                            V1EnvVar(name="POLL_STATE_FILE", value=self.settings.poll_state_file),
                            V1EnvVar(name="POLL_HASH_SALT", value=self.settings.poll_hash_salt),
                        ],
                    )
                ],
            ),
        )
        LOGGER.info("Creating poll pod %s", self.settings.poll_pod_name)
        self.core.create_namespaced_pod(self.namespace, pod)
        self._wait_until_running()

    def _exec(self, *args: str) -> dict:
        self._wait_until_running()
        command = [*self.worker_command, *args]
        LOGGER.info("Executing poll worker action %s", args[0] if args else "")
        response = stream.stream(
            self.core.connect_get_namespaced_pod_exec,
            self.settings.poll_pod_name,
            self.namespace,
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _request_timeout=self.timeout_seconds,
        )
        try:
            return json.loads(response or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Poll worker returned non-JSON output: {response}") from exc

    def _status(self) -> dict:
        try:
            pod = self.core.read_namespaced_pod(self.settings.poll_pod_name, self.namespace)
        except ApiException as exc:
            if exc.status == 404:
                return {"exists": False}
            raise
        if pod.metadata.deletion_timestamp or pod.status.phase in {"Failed", "Succeeded"}:
            LOGGER.info("Removing inactive poll pod %s in phase %s", self.settings.poll_pod_name, pod.status.phase)
            self._delete_pod()
            self._wait_until_deleted()
            return {"exists": False}
        try:
            return self._exec("status")
        except Exception:
            LOGGER.exception("Failed to read poll status")
            return {"exists": True}

    def _wait_until_running(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            pod = self.core.read_namespaced_pod(self.settings.poll_pod_name, self.namespace)
            if pod.status.phase == "Running":
                return
            if pod.status.phase in {"Failed", "Succeeded"}:
                raise RuntimeError(f"Pod {self.settings.poll_pod_name} ended before it could handle poll")
            time.sleep(1)
        raise TimeoutError(f"Timed out waiting for pod {self.settings.poll_pod_name} to run")

    def _creator_phone(self) -> str | None:
        try:
            pod = self.core.read_namespaced_pod(self.settings.poll_pod_name, self.namespace)
        except ApiException:
            return None
        return (pod.metadata.annotations or {}).get(self.creator_annotation)

    def _delete_pod(self) -> None:
        try:
            self.core.delete_namespaced_pod(self.settings.poll_pod_name, self.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def _wait_until_deleted(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            try:
                self.core.read_namespaced_pod(self.settings.poll_pod_name, self.namespace)
            except ApiException as exc:
                if exc.status == 404:
                    return
                raise
            time.sleep(1)
        raise TimeoutError(f"Timed out waiting for pod {self.settings.poll_pod_name} to delete")

    @staticmethod
    def _load_kube_config() -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
