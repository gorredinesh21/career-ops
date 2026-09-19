FROM python:3.10-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY templates/ templates/
COPY static/ static/
# Jobs-only snapshot baked into the image; merged into the live DB at boot
COPY deploy/careerops.db /app/seed/careerops.db
ENV CAREEROPS_SEED=/app/seed/careerops.db
# DB always on local disk (never a network mount); durability via GCS backup
ENV CAREEROPS_DB=/tmp/careerops/careerops.db
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
