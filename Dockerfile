# Multi-stage Dockerfile: build frontend, then Python backend
# Build frontend
FROM node:22-alpine AS frontend-build
WORKDIR /src/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build

# Build backend image
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# tzdata lets the TZ variable set the local date used for daily reports
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Copy frontend build into Flask static folder so Flask can serve it
COPY --from=frontend-build /src/frontend/dist /app/frontend_build

# Run as a non-root user that owns the database directory
RUN useradd --system --no-create-home pos \
    && mkdir -p /data \
    && chown pos:pos /data
USER pos

ENV DB_FILE=/data/church_pos.db
ENV FLASK_STATIC_DIR=/app/frontend_build

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/health', timeout=4)"

# One worker keeps SQLite writes and the login lockout in a single process.
# The control socket isn't used, and the app user has no home folder for it.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app", "--workers", "1", "--threads", "4", "--access-logfile", "-", "--no-control-socket"]
