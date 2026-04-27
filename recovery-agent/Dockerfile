FROM python:3.11-slim

# Install the Docker CLI so recovery-agent can run "docker restart/stop/start".
# The Docker DAEMON is NOT installed here — we use the host daemon via the
# mounted socket (/var/run/docker.sock).
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8003

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]