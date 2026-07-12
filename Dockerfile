FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scanner.py api.py push.py dashboard.py ./
COPY webapp/ ./webapp/
CMD python scanner.py --loop --interval 300 & exec uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}
