FROM python:3.11-slim

WORKDIR /app

# Copy the project
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner.py api.py push.py dashboard.py ./
COPY webapp/ ./webapp/

# Render assigns a PORT env var; the app needs to listen on it (not 127.0.0.1)
# healthcheck: curl the /api/health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health').read()" || exit 1

# Startup: run the scanner poller in the background, then the API server in foreground
CMD [ \
  "sh", "-c", \
  "python scanner.py --loop --interval 300 &  && \
   uvicorn api:app --host 0.0.0.0 --port $PORT" \
]
