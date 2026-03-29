# syntax=docker/dockerfile:1

# ---- Build stage ----
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir --user .

# ---- Runtime stage ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd -r commandclaw && \
    useradd -r -g commandclaw -d /home/commandclaw -m commandclaw && \
    mkdir -p /home/commandclaw/.commandclaw && \
    chown -R commandclaw:commandclaw /home/commandclaw

COPY --from=builder /root/.local /home/commandclaw/.local
ENV PATH="/home/commandclaw/.local/bin:$PATH"

COPY logo.png /app/logo.png

WORKDIR /app
USER commandclaw

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8420/health')"]

ENTRYPOINT ["commandclaw-mcp"]
