# SMS ChatGPT

`sms-chatgpt` is a Python daemon that watches SMS messages from a phone or GSM modem attached over USB. Each sender gets an isolated Kubernetes pod. The daemon sends the incoming text into that pod, the pod asks an LLM for a reply, and the daemon sends the reply back by SMS. Replies are capped at 140 characters. Pods are deleted after 60 seconds of inactivity.

## How It Works

1. A user sends an SMS to the USB-attached phone/modem.
2. The daemon polls unread SMS messages.
3. For each sender, it creates or reuses a Kubernetes pod named `sms-chat-<hash>`.
4. The daemon runs `sms-chatgpt-worker --message ...` inside that pod.
5. The worker calls the configured LLM and returns a <=140 character response.
6. The daemon sends the response by SMS.
7. A cleanup loop deletes pods that have been idle for more than `CHAT_POD_IDLE_SECONDS`.

## Important Hardware Note

Most Android/iPhone handsets do not expose SMS over USB as a simple serial modem. This project expects a GSM modem, LTE dongle, or phone that exposes an AT-command serial interface such as `/dev/ttyUSB0`. If your phone only supports ADB, add a new transport in `sms_chatgpt/sms.py`.

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

## Kubernetes Setup

Build and publish an image that contains this project:

```bash
docker build -t sms-chatgpt:latest .
```

For a local cluster such as kind or minikube, load the image into the cluster or publish it to a registry and set `CHAT_POD_IMAGE`.

The daemon needs permission to create, list, patch, exec into, and delete pods in `KUBERNETES_NAMESPACE`.

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
    verbs: ["create"]
```

## Real SMS Modem

Set:

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

## Environment

See `.env.example` for all settings. The pod receives `LLM_PROVIDER`, `OPENAI_API_KEY`, and `OPENAI_MODEL` from the daemon environment.
