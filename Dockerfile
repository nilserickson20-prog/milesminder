FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata build-essential sqlite3 \
  && rm -rf /var/lib/apt/lists/*

# Workdir
WORKDIR /app

# Copy code first to leverage docker layer caching
COPY milesminder/ milesminder/
COPY pyproject.toml requirements.txt* ./

# Install deps
# Prefer requirements.txt; if you only have pyproject, swap the pip command accordingly.
RUN pip install --no-cache-dir -r requirements.txt \
    || pip install --no-cache-dir .

# Ensure data dir exists (Fly volume will mount here if configured)
RUN mkdir -p /data

# Non-root (optional but recommended)
RUN useradd -m botuser
USER botuser

# Run
CMD ["python", "-m", "milesminder.bot"]
