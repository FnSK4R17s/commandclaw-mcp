FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "fastmcp>=2.0.0"

COPY server.py /app/server.py
WORKDIR /app

EXPOSE 8001

CMD ["python", "server.py"]
