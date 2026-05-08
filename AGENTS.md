# AGENTS.md

## Setup

- Requires Python `>=3.11`; project metadata is in `pyproject.toml`.
- Create a local environment and install the package:
  ```bash
  python -m venv .venv
  . .venv/bin/activate
  pip install -e .
  cp .env.example .env
  ```
- Run locally with mock SMS:
  ```bash
  printf '+15551234567|hello there\n' >> mock-inbox.txt
  SMS_BACKEND=mock SESSION_BACKEND=local LLM_PROVIDER=echo sms-chatgpt-daemon
  ```
- ADB diagnostics:
  ```bash
  adb devices -l
  python3 -m sms_chatgpt.diagnose_adb
  ```
- Build images:
  ```bash
  docker build -f Dockerfile.daemon -t sms-chatgpt-daemon:latest .
  docker build -f Dockerfile -t sms-chatgpt-worker:latest .
  ```

## Testing

- Run the test suite:
  ```bash
  python3 -m unittest discover -s tests
  ```
- Representative tests live in `tests/test_messages.py` and cover SMS reply clamping, ADB row parsing, ADB state files, worker command selection, and worker history persistence.
- For worker smoke tests without OpenAI:
  ```bash
  LLM_PROVIDER=echo python3 -m sms_chatgpt.worker --message hello
  ```

## Style

- Keep Python simple and standard-library first; current tests use `unittest`.
- Preserve the 140-character SMS reply contract through `clamp_sms_reply`.
- Prefer environment-driven configuration in `sms_chatgpt/config.py` and document new settings in `.env.example`.
- Keep optional heavy dependencies lazy where practical, as done for `kubernetes`, `openai`, and `dotenv`.
- Worker containers should continue to support `python -m sms_chatgpt.worker`.

## Review Guidelines

- Check for committed secrets, especially `.env`, OpenAI keys, and generated SMS state files.
- Verify Kubernetes RBAC still grants only namespace-scoped permissions needed for pods and `pods/exec`.
- For ADB changes, confirm both mock/local mode and the Android ADB path are still understandable from the README.
- For chat behavior changes, check that conversation history remains per-sender and is lost when the idle pod is deleted.
- Before approving deploy-related changes, run tests and inspect `Dockerfile`, `Dockerfile.daemon`, `Jenkinsfile`, and `k8s/*.yaml` for consistency.
