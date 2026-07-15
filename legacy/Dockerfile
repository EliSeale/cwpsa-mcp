FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY openapi-spec-pruned.json .
COPY business_knowledge.py .
COPY business-knowledge/ business-knowledge/
COPY Server.py .

ENV MCP_TRANSPORT=http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8080

EXPOSE 8080

CMD ["python", "Server.py"]
