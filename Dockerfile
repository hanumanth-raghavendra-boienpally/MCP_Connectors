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
# Hosts like Render set PORT themselves; the connector reads it.
CMD ["sh", "-c", "python -m connectors.${CONNECTOR}.server"]
