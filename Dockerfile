FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata build-essential sqlite3 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy code first
COPY milesminder/ milesminder/

COPY assets/ /app/assets/

# Install deps
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Data dir (Fly volume mounts here)
RUN mkdir -p /data

# Optional: run as non-root
RUN useradd -m botuser
USER root

CMD ["python", "-m", "milesminder.bot"]
