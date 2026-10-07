FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POSTGRES_HOST=db \
    POSTGRES_PORT=5432 \
    POSTGRES_USER=policy \
    POSTGRES_PASSWORD=policy \
    POSTGRES_DB=policy

WORKDIR /app

# curl 供容器内 HEALTHCHECK 使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=6 --start-period=20s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
