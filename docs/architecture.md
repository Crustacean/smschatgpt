# SMS ChatGPT Architecture

## Runtime Flow

```mermaid
flowchart LR
    sender[SMS sender phone]
    android[Android phone<br/>SIM + SMS inbox]
    host[Host laptop<br/>ADB server]

    subgraph cluster[Kubernetes cluster]
        daemon[Daemon pod<br/>sms_chatgpt.daemon]
        config[ConfigMap<br/>runtime settings]
        secret[Secret<br/>OPENAI_API_KEY]
        rbac[RBAC<br/>pods + pods/exec]

        subgraph sessions[Per-sender chat pods]
            workerA[chat pod<br/>sms-chat-&lt;sender-hash&gt;]
            history[(conversation history<br/>/tmp/sms-chatgpt-history.json)]
        end
    end

    openai[OpenAI API]

    sender -->|SMS| android
    android <-->|USB debugging| host
    host <-->|ADB_SERVER_SOCKET<br/>tcp:host.minikube.internal:5037| daemon

    config --> daemon
    secret --> daemon
    rbac --> daemon

    daemon -->|poll content://sms/inbox| host
    daemon -->|create/reuse pod| workerA
    daemon -->|kubectl exec<br/>python -m sms_chatgpt.worker| workerA

    workerA <-->|load/save turns| history
    workerA -->|prompt + recent history| openai
    openai -->|&lt;=140 char reply| workerA
    workerA -->|reply text| daemon

    daemon -->|ADB compose intent| host
    host -->|opens SMS composer| android
    android -->|tap Send / helper send| sender

    daemon -.->|delete after idle timeout| workerA
```

## Deployment Flow

```mermaid
flowchart TD
    repo[GitHub repository]
    jenkins[Jenkins pipeline]
    tests[Unit tests]
    daemonImage[Daemon image<br/>Dockerfile.daemon]
    workerImage[Worker image<br/>Dockerfile]
    registry[Docker Hub]
    manifests[Kubernetes manifests<br/>k8s/*.yaml]
    cluster[Kubernetes namespace<br/>sandbox/dev/uat/prod]
    secret[OpenAI secret<br/>openai-api-key credential]

    repo --> jenkins
    jenkins --> tests
    tests --> daemonImage
    tests --> workerImage
    daemonImage --> registry
    workerImage --> registry
    jenkins --> manifests
    jenkins --> secret
    registry --> cluster
    manifests --> cluster
    secret --> cluster
```

## Key Runtime Notes

- The Android phone is physically attached to the host machine.
- In minikube, the pod connects to the host ADB server through `ADB_SERVER_SOCKET=tcp:host.minikube.internal:5037`.
- The daemon pod reads inbound SMS over ADB and opens the SMS composer for outbound replies unless a device-specific silent-send template is configured.
- Each sender maps to one Kubernetes chat pod, named from a sender hash.
- Conversation memory is stored inside that sender's chat pod and disappears when the pod is deleted after `CHAT_POD_IDLE_SECONDS`.
- The daemon needs RBAC permissions for pods and `pods/exec` so it can create chat pods and run the worker inside them.
