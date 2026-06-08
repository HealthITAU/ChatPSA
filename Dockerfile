FROM python:3.12-slim

WORKDIR /app

# Install gosu for reliable privilege dropping and app dependencies
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./
COPY changelog.json ./
COPY templates/ templates/
COPY static/ static/

# Create data directory and non-root user (with home dir so gunicorn/python don't complain)
RUN mkdir -p /data && \
    adduser --system --home /home/appuser appuser && \
    chown appuser /data

# Entrypoint: fix /data ownership then drop to appuser
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose the web port
EXPOSE 5001

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5001/healthz')" || exit 1

# Entrypoint runs as root to fix volume perms, then drops to appuser
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--timeout", "120", "app:app"]
