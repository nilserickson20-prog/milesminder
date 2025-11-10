FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata build-essential sqlite3 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy app code first (so edits rebuild only these layers)
COPY milesminder/ milesminder/

# Install Python deps from requirements.txt (repo root)
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Ensure data dir exists (Fly volume will mount here if configured)
RUN mkdir -p /data

# (Optional) run as non-root
RUN useradd -m botuser
USER botuser

CMD ["python", "-m", "milesminder.bot"]

