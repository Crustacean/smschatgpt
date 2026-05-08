from __future__ import annotations

import ast
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
from .polls import (
    ACTIVE,
    CLOSED,
    PENDING,
    PollState,
    classify_vote,
    contains_poll_intent,
    hash_msisdn,
    match_vote_option,
    parse_creator_command,
)

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
    creator_hash_annotation = "sms-chatgpt/creator-hash"
    label_selector = "app=sms-chatgpt-poll,managed-by=sms-chatgpt-daemon"

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
        statuses = self._statuses()
        own = self._own_status(statuses, sender_hash)

        if contains_poll_intent(body, self.settings.poll_keywords):
            if own and (own.get("state") or {}).get("status") in {PENDING, ACTIVE, CLOSED}:
                return PollResponse(True, "You have an ongoing poll.")
            pod_name = self._pod_name(sender_hash)
            self._ensure_pod(pod_name, sender, sender_hash)
            result = self._exec(pod_name, "draft", "--creator-hash", sender_hash, "--message", body)
            return PollResponse(True, result.get("reply"))

        if own and (own.get("state") or {}).get("status") == PENDING:
            command, _details = parse_creator_command(body)
            if command == "confirm":
                result = self._exec(own["pod_name"], "confirm")
            elif command == "cancel":
                result = self._exec(own["pod_name"], "cancel")
                self._delete_pod(own["pod_name"])
            elif body.strip().lower().startswith("amend "):
                result = self._exec(own["pod_name"], "amend", "--message", body)
            else:
                vote_response = self._handle_vote(sender_hash, body, statuses)
                if vote_response.handled:
                    return vote_response
                result = self._exec(own["pod_name"], "amend", "--message", body)
            return PollResponse(bool(result.get("handled", True)), result.get("reply"))

        vote_response = self._handle_vote(sender_hash, body, statuses)
        if vote_response.handled:
            return vote_response

        if any(match_vote_option(body, (item.get("state") or {}).get("options") or []) for item in statuses if (item.get("state") or {}).get("status") == PENDING):
            return PollResponse(True, "Poll is not open yet.")

        return PollResponse(False)

    def close_expired(self) -> list[OutboundSms]:
        outbound: list[OutboundSms] = []
        for status in self._statuses():
            state = status.get("state") or {}
            creator = status.get("creator_phone")
            pod_name = status["pod_name"]
            if state.get("status") == CLOSED and state.get("result_reply") and creator:
                outbound.append(OutboundSms(creator, clamp_sms_reply(state["result_reply"]), pod_name))
                continue
            if not status.get("expired"):
                continue
            result = self._exec(pod_name, "finalize")
            if creator and result.get("reply"):
                outbound.append(OutboundSms(creator, clamp_sms_reply(result["reply"]), pod_name))
        return outbound

    def ack_results_sent(self, outbound_messages: list[OutboundSms] | None = None) -> None:
        for outbound in outbound_messages or []:
            if outbound.poll_id:
                self._delete_pod(outbound.poll_id)

    def _ensure_pod(self, pod_name: str, creator_phone: str, creator_hash: str) -> None:
        try:
            existing = self.core.read_namespaced_pod(pod_name, self.namespace)
            if existing.metadata.deletion_timestamp or existing.status.phase in {"Failed", "Succeeded"}:
                self._delete_pod(pod_name)
                self._wait_until_deleted(pod_name)
            else:
                return
        except ApiException as exc:
            if exc.status != 404:
                raise

        pod = V1Pod(
            metadata=V1ObjectMeta(
                name=pod_name,
                labels={
                    "app": "sms-chatgpt-poll",
                    "managed-by": "sms-chatgpt-daemon",
                    "creator": creator_hash[:16],
                },
                annotations={
                    self.creator_annotation: creator_phone,
                    self.creator_hash_annotation: creator_hash,
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
        LOGGER.info("Creating poll pod %s", pod_name)
        self.core.create_namespaced_pod(self.namespace, pod)
        self._wait_until_running(pod_name)

    def _exec(self, pod_name: str, *args: str) -> dict:
        self._wait_until_running(pod_name)
        command = [*self.worker_command, *args]
        LOGGER.info("Executing poll worker action %s", args[0] if args else "")
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
        return self._parse_worker_response(response)

    def _statuses(self) -> list[dict]:
        pods = self.core.list_namespaced_pod(namespace=self.namespace, label_selector=self.label_selector)
        statuses: list[dict] = []
        for pod in pods.items:
            status = self._status(pod.metadata.name)
            if status.get("exists"):
                status["pod_name"] = pod.metadata.name
                status["creator_phone"] = (pod.metadata.annotations or {}).get(self.creator_annotation)
                status["creator_hash"] = (pod.metadata.annotations or {}).get(self.creator_hash_annotation)
                statuses.append(status)
        return statuses

    def _status(self, pod_name: str) -> dict:
        try:
            pod = self.core.read_namespaced_pod(pod_name, self.namespace)
        except ApiException as exc:
            if exc.status == 404:
                return {"exists": False}
            raise
        if pod.metadata.deletion_timestamp or pod.status.phase in {"Failed", "Succeeded"}:
            LOGGER.info("Removing inactive poll pod %s in phase %s", pod_name, pod.status.phase)
            self._delete_pod(pod_name)
            self._wait_until_deleted(pod_name)
            return {"exists": False}
        try:
            return self._exec(pod_name, "status")
        except Exception:
            LOGGER.exception("Failed to read poll status for %s", pod_name)
            return {"exists": True}

    def _handle_vote(self, sender_hash: str, body: str, statuses: list[dict]) -> PollResponse:
        other_matches: list[tuple[dict, PollState, object]] = []
        own_match = None
        for status in statuses:
            state_data = status.get("state") or {}
            if state_data.get("status") != ACTIVE:
                continue
            state = PollState.from_dict(state_data)
            decision = classify_vote(body, state, sender_hash)
            if decision.kind == "ask":
                continue
            if state.creator_hash == sender_hash:
                own_match = (status, state, decision)
            else:
                other_matches.append((status, state, decision))

        if len(other_matches) > 1:
            return PollResponse(True, "Multiple polls match. Reply with a clearer vote.")
        if len(other_matches) == 1:
            status, state, decision = other_matches[0]
            if state.is_expired():
                return PollResponse(True, "This poll is closed.")
            result = self._exec(status["pod_name"], "vote", "--voter-hash", sender_hash, "--message", body)
            if result.get("route_to_chat"):
                return PollResponse(False)
            return PollResponse(bool(result.get("handled", True)), result.get("reply"))
        if own_match:
            return PollResponse(True, "Poll creators cannot vote in their own poll.")
        return PollResponse(False)

    @staticmethod
    def _own_status(statuses: list[dict], creator_hash: str) -> dict | None:
        for status in statuses:
            state = status.get("state") or {}
            if state.get("creator_hash") == creator_hash or status.get("creator_hash") == creator_hash:
                return status
        return None

    def _wait_until_running(self, pod_name: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            pod = self.core.read_namespaced_pod(pod_name, self.namespace)
            if pod.status.phase == "Running":
                return
            if pod.status.phase in {"Failed", "Succeeded"}:
                raise RuntimeError(f"Pod {pod_name} ended before it could handle poll")
            time.sleep(1)
        raise TimeoutError(f"Timed out waiting for pod {pod_name} to run")

    def _delete_pod(self, pod_name: str) -> None:
        try:
            self.core.delete_namespaced_pod(pod_name, self.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def _pod_name(self, creator_hash: str) -> str:
        base = self.settings.poll_pod_name[:46].rstrip("-")
        return f"{base}-{creator_hash[:16]}"

    @staticmethod
    def _parse_worker_response(response: str) -> dict:
        text = (response or "{}").strip()
        candidates = [text]
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                try:
                    parsed = ast.literal_eval(candidate)
                except (SyntaxError, ValueError):
                    continue
            if isinstance(parsed, dict):
                return parsed
        raise RuntimeError(f"Poll worker returned unparseable output: {response}")

    def _wait_until_deleted(self, pod_name: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            try:
                self.core.read_namespaced_pod(pod_name, self.namespace)
            except ApiException as exc:
                if exc.status == 404:
                    return
                raise
            time.sleep(1)
        raise TimeoutError(f"Timed out waiting for pod {pod_name} to delete")

    @staticmethod
    def _load_kube_config() -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
