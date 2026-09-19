FROM python:3.10-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY templates/ templates/
COPY static/ static/
# Jobs-only snapshot at a path the volume mount can never shadow
COPY deploy/careerops.db /app/seed/careerops.db
ENV CAREEROPS_SEED=/app/seed/careerops.db
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
