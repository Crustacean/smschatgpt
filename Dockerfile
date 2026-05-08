FROM python:3.12-alpine

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-compile "openai>=1.40.0"
RUN printf '#!/bin/sh\nexec python -m sms_chatgpt.worker "$@"\n' > /usr/local/bin/sms-chatgpt-worker \
    && printf '#!/bin/sh\nexec python -m sms_chatgpt.poll_worker "$@"\n' > /usr/local/bin/sms-chatgpt-poll-worker \
    && chmod +x /usr/local/bin/sms-chatgpt-worker /usr/local/bin/sms-chatgpt-poll-worker

COPY sms_chatgpt/__init__.py \
     sms_chatgpt/config.py \
     sms_chatgpt/llm.py \
     sms_chatgpt/messages.py \
     sms_chatgpt/polls.py \
     sms_chatgpt/poll_worker.py \
     sms_chatgpt/worker.py \
     ./sms_chatgpt/

ENTRYPOINT ["python", "-m", "sms_chatgpt.worker"]
