FROM python:3.11-slim

# set the working directory inside the container
WORKDIR /app

# make Python behave nicely in containers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# copy dependencies and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy your bot code into the container
COPY milesminder ./milesminder

# create a folder for the SQLite database
VOLUME ["/data"]

# start the bot
CMD ["python", "-m", "milesminder.bot"]
