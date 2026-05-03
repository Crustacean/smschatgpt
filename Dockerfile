FROM python:3.12-alpine

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir \
    "openai>=1.40.0" \
    "python-dotenv>=1.0.1"

COPY sms_chatgpt ./sms_chatgpt

ENTRYPOINT ["python", "-m", "sms_chatgpt.worker"]
