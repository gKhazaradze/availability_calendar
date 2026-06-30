# Single-container image: gunicorn runs the Flask app, which serves both the
# JSON API and the static frontend. `docker compose up -d` and you're done.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AVAILABILITY_DB=/app/data/avail.db

WORKDIR /app

# Install Python deps first so this layer is cached across code changes
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Application code
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# The SQLite DB lives on a mounted volume so it survives restarts
RUN mkdir -p /app/data

WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
