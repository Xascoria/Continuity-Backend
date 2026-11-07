FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend
COPY image/original.txt image/original.txt
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Local docker mounts ./data (SQLite) and ./secrets. Fly leaves DATABASE_URL
# and ADMIN_USERNAME / ADMIN_PASSWORD as secrets and does not mount either.
ENV DB_PATH=/app/data/app.db \
    ADMIN_SECRET_FILE=/app/secrets/admin.json

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT','8000'))"

# Listens on 8000 unless PORT is set (Fly sets it to match internal_port).
ENTRYPOINT ["/docker-entrypoint.sh"]
