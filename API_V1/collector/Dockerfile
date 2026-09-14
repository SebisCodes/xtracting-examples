# Fully qualified: Docker prepends docker.io to a short name, Podman does not
# and fails to resolve it.
FROM docker.io/python:3.12-slim

# psycopg[binary] ships its own libpq, so there is nothing to apt-get here.
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Unbuffered, so `docker compose logs -f` shows what is happening as it happens
# rather than in blocks whenever the buffer fills.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Runs as a non-root user: this container holds API keys in its environment and
# has no reason to be able to write anywhere.
RUN useradd --create-home --uid 10001 collector && chown -R collector /srv
USER collector

CMD ["python", "-m", "app.main"]
