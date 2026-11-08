FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py .
ENV PYTHONUNBUFFERED=1 \
    SCHEDULE_FILE=/data/schedules.json
EXPOSE 8080
CMD ["python", "server.py"]
