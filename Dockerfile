FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Registry artifact (built offline by CI — see scripts/build_registry.py)
COPY registry.json .

# Business-knowledge OKF bundle
COPY business-knowledge/ business-knowledge/

# Source package
COPY src/ src/

ENV PYTHONPATH=/app/src
ENV MCP_TRANSPORT=http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

CMD ["python", "-m", "cwpsa.server"]
