# docker build -t mcp-elasticsearch .
FROM python:3.12-slim

WORKDIR /app
# Install the exact library versions pinned in requirements.txt (the ones the
# security scan checked), not whatever is newest on the day of the build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY connector_core ./connector_core
COPY connectors ./connectors

ENV HOST=0.0.0.0 PORT=8000
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
# Docker marks the container unhealthy if /health stops answering.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('PORT', '8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + port + '/health', timeout=4).read() == b'ok' else 1)"]
# Hosts like Render set PORT themselves; the connector reads it.
CMD ["python", "-m", "connectors.elasticsearch.server"]
