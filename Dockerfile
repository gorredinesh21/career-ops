FROM python:3.10-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY templates/ templates/
COPY static/ static/
RUN mkdir -p data && echo placeholder > data/.keep
# Public job intelligence DB baked at build time (no user data; see deploy notes)
COPY deploy/careerops.db data/careerops.db
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
