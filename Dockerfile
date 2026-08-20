FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Mount your data folder as a volume so you can swap the CSV without rebuilding the image:
#   docker run -v $(pwd)/data:/app/data -v $(pwd)/.env:/app/.env -p 8000:8000 wcvp-api
VOLUME ["/app/data"]

EXPOSE 8000

# Gunicorn managing uvicorn workers = production-grade process management
# (auto-restarts a worker if it crashes, handles concurrency).
CMD ["gunicorn", "app.main:app", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--timeout", "120"]
