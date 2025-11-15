# Multi-stage Dockerfile: build frontend, then Python backend
# Build frontend
FROM node:18-alpine AS frontend-build
WORKDIR /src/frontend
COPY frontend/package*.json ./
# If you have a lockfile, copy it as well
COPY frontend/yarn.lock ./
RUN npm ci --legacy-peer-deps
COPY frontend/ .
RUN npm run build

# Build backend image
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install OS deps for building wheels (if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy backend files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Add gunicorn to requirements.txt or install it here:
RUN pip install --no-cache-dir gunicorn

COPY . /app

# Copy frontend build into Flask static folder so Flask can serve it
# (Assumes Flask static folder is /app/frontend_build)
COPY --from=frontend-build /src/frontend/dist /app/frontend_build

# Ensure DB directory exists for mounted volume
RUN mkdir -p /data
ENV DB_FILE=/data/church_pos.db
ENV FLASK_STATIC_DIR=/app/frontend_build

# Expose port and run with gunicorn
EXPOSE 5000
# Use a simple worker count; adjust for your host
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app", "--workers", "2", "--threads", "4"]