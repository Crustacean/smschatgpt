# SMS ChatGPT Architecture

## Runtime Flow

```mermaid
flowchart LR
    sender[SMS sender phone]
    android[Android phone<br/>SIM + SMS inbox]
    host[Host laptop<br/>ADB server]
    openai[OpenAI API]

    subgraph cluster[Kubernetes cluster]
        daemon[Daemon pod<br/>sms_chatgpt.daemon]
        router{Poll router}
        config[ConfigMap<br/>runtime settings]
        secret[Secret<br/>OPENAI_API_KEY<br/>POLL_HASH_SALT]
        rbac[RBAC<br/>pods + pods/exec]

        subgraph sessions[Per-sender chat pods]
            workerA[chat pod<br/>sms-chat-&lt;sender-hash&gt;]
            history[(conversation history<br/>/tmp/sms-chatgpt-history.json)]
        end

        subgraph polls[Per-creator poll pods]
            pollPod[poll pod<br/>sms-poll-active-&lt;creator-hash&gt;]
            pollWorker[poll worker<br/>sms_chatgpt.poll_worker]
            pollState[(poll state<br/>/tmp/sms-chatgpt-poll.json)]
        end
    end

    sender -->|SMS| android
    android <-->|USB debugging| host
    host <-->|ADB_SERVER_SOCKET<br/>tcp:host.minikube.internal:5037| daemon

    config --> daemon
    secret --> daemon
    rbac --> daemon

    daemon -->|poll content://sms/inbox| host
    daemon -->|inbound SMS| router

    router -->|normal ask| workerA
    daemon -->|create/reuse chat pod| workerA
    daemon -->|exec<br/>python -m sms_chatgpt.worker| workerA

    workerA <-->|load/save turns| history
    workerA -->|prompt + recent history| openai
    openai -->|&lt;=140 char reply| workerA
    workerA -->|reply text| daemon

    router -->|poll intent / creator command / contextual vote| pollPod
    router -.->|context-free vote waits for clarification| daemon
    daemon -->|create/read/list poll pods| pollPod
    daemon -->|exec draft/amend/confirm<br/>vote/status/finalize| pollWorker
    pollPod --- pollWorker
    pollWorker <-->|load/save| pollState
    pollWorker -->|draft extraction<br/>result summary| openai
    openai -->|poll draft/result text| pollWorker
    pollWorker -->|vote ack / draft / result| daemon

    daemon -->|ADB compose intent| host
    host -->|opens SMS composer| android
    android -->|tap Send / helper send| sender

    daemon -.->|delete after idle timeout| workerA
    daemon -.->|delete after result sent| pollPod
```

## Deployment Flow

```mermaid
flowchart TD
    repo[GitHub repository]
    jenkins[Jenkins pipeline]
    tests[Unit tests]
    daemonImage[Daemon image<br/>Dockerfile.daemon]
    workerImage[Worker image<br/>Dockerfile<br/>chat + poll worker]
    registry[Docker Hub]
    manifests[Kubernetes manifests<br/>k8s/*.yaml]
    cluster[Kubernetes namespace<br/>sandbox/dev/uat/prod]
    secret[Runtime secrets<br/>openai-api-key<br/>poll-hash-salt]

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
- When `POLL_ENABLED=true`, inbound SMS first passes through the poll router before falling back to the normal ChatGPT flow.
- A poll request containing words such as `poll`, `vote`, or `voting` creates a per-creator poll pod named from `POLL_POD_NAME` plus the creator MSISDN hash prefix.
- Each creator hash can have one pending or active poll. Other MSISDNs can still create their own polls and vote in polls created by others.
- Poll pods run the worker image and execute `python -m sms_chatgpt.poll_worker` for `draft`, `amend`, `confirm`, `vote`, `status`, and `finalize` actions.
- Poll state is stored inside the poll pod at `POLL_STATE_FILE`. It stores the creator hash, question, options, duration, expiry, and votes keyed by voter hash, not raw voter MSISDNs.
- The creator cannot vote in their own poll. Each voter hash can vote once per poll, and natural-language vote matching checks the vote text against the poll question context.
- Context-free vote-like SMS such as `yes`, `no`, `1`, or `maybe` are held in the daemon as pending votes. The sender must provide context before the matched poll expires, otherwise the pending vote is discarded.
- On each daemon loop, expired polls are finalized, anonymous aggregate counts are summarized through OpenAI, the result is sent only to the creator, and the poll pod is deleted after the send is acknowledged.
- The daemon needs RBAC permissions for pods and `pods/exec` so it can create chat and poll pods, inspect their status, execute workers inside them, patch metadata, and delete them.
