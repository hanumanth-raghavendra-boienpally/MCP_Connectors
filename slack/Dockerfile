# One image per connector: docker build --build-arg CONNECTOR=slack -t mcp-slack .
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir "fastmcp>=4.1,<5" "httpx>=0.27"

COPY connector_core ./connector_core
COPY connectors ./connectors

ARG CONNECTOR=slack
ENV CONNECTOR=${CONNECTOR} HOST=0.0.0.0 PORT=8000
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
# Docker marks the container unhealthy if /health stops answering.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('PORT', '8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + port + '/health', timeout=4).read() == b'ok' else 1)"]
# Hosts like Render set PORT themselves; the connector reads it.
CMD ["sh", "-c", "python -m connectors.${CONNECTOR}.server"]
