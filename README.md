# SMS ChatGPT

`sms-chatgpt` is a Python daemon that watches SMS messages from an Android phone over ADB. Each sender gets an isolated Kubernetes pod. The daemon sends the incoming text into that pod, the pod asks an LLM for a reply, and the daemon sends the reply back by SMS. Replies are capped at 140 characters. Pods are deleted after 60 seconds of inactivity.

## How It Works

1. A user sends an SMS to the USB-attached Android phone.
2. The daemon polls unread SMS messages.
3. For each sender, it creates or reuses a Kubernetes pod named `sms-chat-<hash>`.
4. The daemon runs `python -m sms_chatgpt.worker --message ...` inside that pod.
5. The worker loads that pod's conversation history, calls the configured LLM, saves the new turn, and returns a <=140 character response.
6. The daemon sends the response by SMS.
7. A cleanup loop deletes pods that have been idle for more than `CHAT_POD_IDLE_SECONDS`.

## SMS Polls

When `POLL_ENABLED=true`, inbound messages containing keywords such as `poll`, `vote`, or `voting` start the poll flow instead of the normal chat flow.

Example creator SMS:

```text
Create a Yes or No poll on funding to dig a local well for 60 seconds
```

The daemon creates a pending poll and replies with a draft. The creator can then reply:

- `YES`, `CONFIRM`, `OK`, `APPROVE`, or `START` to open the poll.
- `AMEND <new wording/options/duration>` to revise it.
- `CANCEL` to discard it.

While a poll is active, any sender except the creator can vote once by replying with the option number, such as `1`, or the exact option text. The creator's vote is rejected, and duplicate votes from the same MSISDN hash keep the first vote. Messages that do not match the active poll choices continue through the normal ChatGPT flow.

Poll state is stored in a dedicated poll pod named by `POLL_POD_NAME`. The state stores MSISDN hashes, not raw voter phone numbers. When the poll expires, the worker sends only anonymous aggregate counts to OpenAI for a <=140 character summary, sends that result to the creator, and deletes the poll pod.

## Important Android/ADB Note

Android allows ADB shell access to the SMS content provider on some devices/builds, which lets the daemon read inbound SMS with:

```bash
adb shell content query --uri content://sms/inbox
```

Silent SMS sending over ADB is not portable. Some Android/vendor builds expose a shell command or service call that can send SMS, while others block it. This project supports ADB receiving out of the box. By default it opens the SMS composer with the reply filled in; tap Send on the phone to deliver it. For automatic sending, use `ADB_SEND_MODE=template` with `ADB_SEND_COMMAND_TEMPLATE` or install a companion SMS app.

## Quick Start With Mock SMS

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
```

Add a mock inbound SMS:

```bash
printf '+15551234567|hello there\n' >> mock-inbox.txt
```

Run the daemon:

```bash
SMS_BACKEND=mock SESSION_BACKEND=local LLM_PROVIDER=echo sms-chatgpt-daemon
```

Responses are appended to `mock-outbox.txt`.

Set `SESSION_BACKEND=kubernetes` when you want the real pod-per-sender behavior.

## Android/ADB Setup

Enable developer options and USB debugging on the Android phone, then authorize the computer.

Install ADB on the host:

```bash
sudo apt-get install android-tools-adb
```

Confirm the phone is visible:

```bash
adb devices -l
```

Diagnose SMS access:

```bash
python3 -m sms_chatgpt.diagnose_adb
```

If you have more than one Android device attached, set:

```bash
ADB_SERIAL=<device-serial-from-adb-devices>
```

Run the daemon in ADB read mode:

```bash
SMS_BACKEND=adb SESSION_BACKEND=local LLM_PROVIDER=echo sms-chatgpt-daemon
```

The default ADB send mode is:

```bash
ADB_SEND_MODE=compose
```

This opens the SMS app with the generated reply filled in. For diagnostics:

```bash
python3 -m sms_chatgpt.diagnose_adb --send-to +254700000000
```

For non-interactive automatic sending, configure a device-specific command:

```bash
ADB_SEND_MODE=template
ADB_SEND_COMMAND_TEMPLATE='<your command using {adb} {serial_args} {phone} {body}>'
```

Template variables:

- `{adb}`: adb executable, shell-quoted.
- `{serial_args}`: `-s <serial>` when `ADB_SERIAL` is set.
- `{phone}`: destination number, shell-quoted.
- `{body}`: reply body, shell-quoted and capped to 140 characters.

For example, if your device has a helper command or app installed, the template might look like:

```bash
ADB_SEND_COMMAND_TEMPLATE='{adb} {serial_args} shell am broadcast -a com.example.SEND_SMS --es phone {phone} --es body {body}'
```

The exact send command depends on the Android build or helper app you install.

For testing without sending or opening the composer:

```bash
ADB_SEND_MODE=log
```

## Kubernetes Setup

Label the Kubernetes node that has the Android phone attached:

```bash
kubectl label node <node-name> sms-chatgpt.usb-modem=true
```

Build and publish an image that contains this project:

```bash
docker build -f Dockerfile.daemon -t sms-chatgpt-daemon:latest .
docker build -f Dockerfile -t sms-chatgpt-worker:latest .
```

For a local cluster such as kind or minikube, load the image into the cluster or publish it to a registry and set `CHAT_POD_IMAGE`.

The daemon needs permission to create, list, patch, exec into, and delete pods in `KUBERNETES_NAMESPACE`.

Each per-sender pod stores conversation context in `CHAT_HISTORY_FILE` and keeps the most recent `CHAT_HISTORY_MAX_TURNS` user/assistant turns. That history lives only as long as the pod; increase `CHAT_POD_IDLE_SECONDS` if SMS follow-ups should keep context for longer than the default 60 seconds.

Polls use the same worker image for a dedicated poll pod. Set `POLL_HASH_SALT` as a Kubernetes Secret so MSISDN hashes are stable but not reversible from repo configuration.

Example minimal role:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: sms-chatgpt
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["create", "get", "list", "patch", "delete"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create", "get"]
```

The manifests in `k8s/` deploy the daemon, RBAC, config, and namespace. The daemon deployment mounts `/dev/bus/usb` from the node, so the pod is privileged and scheduled only on nodes labeled `sms-chatgpt.usb-modem=true`.

For minikube on a laptop, the host can see the phone even when the pod cannot safely claim USB directly. In that setup, run the ADB server on the host and let the pod connect to it:

```bash
adb kill-server
adb -a start-server
adb devices -l
```

The Kubernetes config uses:

```yaml
ADB_SERVER_SOCKET: "tcp:host.minikube.internal:5037"
```

Keep that ADB server running while the daemon pod is deployed.

## Jenkins Deployment

`Jenkinsfile` runs tests, builds two images, pushes them, and deploys to Kubernetes:

- `Dockerfile.daemon` builds the long-running SMS daemon image.
- `Dockerfile` builds the lightweight worker image used for per-sender chat pods.

Before running the Jenkins job, update `IMAGE_REPOSITORY` in `Jenkinsfile`, then create these Jenkins credentials:

- `docker-registry-credentials`: username/password or token for your container registry.
- `kubeconfig`: kubeconfig file for your cluster.
- `openai-api-key`: secret text containing your OpenAI API key.
- `poll-hash-salt`: secret text used to hash voter MSISDNs.

## AT Modem Alternative

If you use a GSM/LTE modem dongle instead of Android ADB, set:

```bash
SMS_BACKEND=at
SMS_SERIAL_PORT=/dev/ttyUSB0
SMS_BAUDRATE=115200
```

The AT backend uses text mode commands:

- `AT+CMGF=1`
- `AT+CMGL="REC UNREAD"`
- `AT+CMGS="<number>"`
- `AT+CMGD=<index>`

If the daemon does not reply, first test whether the modem exposes SMS:

```bash
python3 -m sms_chatgpt.diagnose_modem --port /dev/ttyUSB0
python3 -m sms_chatgpt.diagnose_modem --port /dev/ttyUSB1
```

If `AT+CMGL="ALL"` shows your messages but the daemon does not process them, run the daemon with:

```bash
LOG_LEVEL=DEBUG SMS_MESSAGE_STATUS=ALL SMS_BACKEND=at SMS_SERIAL_PORT=/dev/ttyUSB0 SESSION_BACKEND=local LLM_PROVIDER=echo sms-chatgpt-daemon
```

Some phones expose SMS in SIM storage and others in phone storage. Test both:

```bash
python3 -m sms_chatgpt.diagnose_modem --port /dev/ttyUSB0 --storage SM
python3 -m sms_chatgpt.diagnose_modem --port /dev/ttyUSB0 --storage ME
```

Then run with the storage that lists inbound messages:

```bash
SMS_STORAGE=SM SMS_MESSAGE_STATUS=ALL SMS_BACKEND=at SMS_SERIAL_PORT=/dev/ttyUSB0 SESSION_BACKEND=local LLM_PROVIDER=echo sms-chatgpt-daemon
```

## Environment

See `.env.example` for all settings. The pod receives `LLM_PROVIDER`, `OPENAI_API_KEY`, and `OPENAI_MODEL` from the daemon environment.
