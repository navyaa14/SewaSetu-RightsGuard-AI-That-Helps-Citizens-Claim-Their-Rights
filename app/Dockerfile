FROM python:3.11-slim

# tzdata: the portal keeps Indian Standard Time wall-clock timestamps (as the seed does)
RUN apt-get update && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Kolkata PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /var/sewasetu/uploads /var/log/sewasetu \
    && crontab /app/deploy/crontab

EXPOSE 8000 8101 8102
# migrations are idempotent; then the portal
CMD ["sh", "-c", "python migrate.py && exec gunicorn --bind 0.0.0.0:8000 --workers 4 --timeout 30 app:app"]
