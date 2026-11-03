# ==============================================================================
# DBOps Control Plane - Standalone Container
# ==============================================================================
FROM python:3.12-slim

# Prevent python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    BACKUP_STORAGE_DIR=/var/backups

# Install official postgresql-client, default-mysql-client, redis-tools, curl, and runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    default-mysql-client \
    redis-tools \
    curl \
    gzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user and backup directory
RUN groupadd -r dbops && useradd -r -g dbops -m -d /home/dbops dbops \
    && mkdir -p /var/backups \
    && chown -R dbops:dbops /var/backups

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY --chown=dbops:dbops app /app/app
COPY --chown=dbops:dbops scripts /app/scripts

# Switch to non-root user
USER dbops

# Expose default management port
EXPOSE 8080

# Healthcheck testing the local control plane health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

