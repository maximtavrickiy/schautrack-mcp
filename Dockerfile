FROM python:3.13-slim

RUN pip install --no-cache-dir "mcp==2.2.0" "httpx==0.28.1" \
    && useradd --create-home --uid 1000 app \
    && mkdir /data && chown app /data

COPY schautrack_mcp.py /app/schautrack_mcp.py

USER app
ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    SCHAUTRACK_MCP_DATA=/data/precise_foods.json \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8000
CMD ["python", "/app/schautrack_mcp.py"]
