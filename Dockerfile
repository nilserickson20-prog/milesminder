# ---------------------------------------------------------------
# 1. Base image
# ---------------------------------------------------------------
FROM python:3.11-slim AS base

# Set working directory
WORKDIR /app

# ---------------------------------------------------------------
# 2. System dependencies
# ---------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------
# 3. Copy project files
# ---------------------------------------------------------------
# Copy requirements (if you have a separate file)
COPY requirements.txt /app/requirements.txt

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . /app

# Copy any reward videos (optional)
# This assumes you have /assets/rewards/... files in your repo
# Remove if you're hosting videos remotely
COPY assets/ /app/assets/

# ---------------------------------------------------------------
# 4. Environment setup
# ---------------------------------------------------------------
# Prevent Python from writing .pyc files and force stdout flush
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ---------------------------------------------------------------
# 5. Entrypoint
# ---------------------------------------------------------------
CMD ["python", "-m", "milesminder.bot"]
