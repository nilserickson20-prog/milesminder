FROM python:3.11-slim

# install tzdata so ZoneInfo("America/New_York") works
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY milesminder ./milesminder

VOLUME ["/data"]
CMD ["python", "-m", "milesminder.bot"]
