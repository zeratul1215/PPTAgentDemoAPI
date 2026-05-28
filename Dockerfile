FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

COPY . /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir -r /app/requirements.txt

CMD ["bash", "-lc", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]

