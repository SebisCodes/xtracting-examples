# Built from the API_V1 folder, not from this folder:
#
#     docker compose build        # the compose file sets context: ..
#
# because the image carries two folders - crawler/ (this service) and crawlkit/
# (the crawl rules and engines shared with ../dashboard/standard). The dashboard runs
# "Test this configuration" with literally the same code this container crawls
# with, which is the only way the two cannot drift apart.
#
# No docker.io prefix on purpose: this image lives on Microsoft's registry, and
# the fully qualified name is what makes it work on Podman as well as Docker.
# The Playwright base is about 4 GB because it ships Chromium; that is what
# sources set to "Rendered" need. If none of your sources needs a browser,
# build on plain Python and every rendered fetch fails with a sentence saying
# exactly that:
#
#     docker compose build --build-arg CRAWLER_BASE=docker.io/python:3.12-slim
#
# The tag must match the playwright pin in crawlkit/requirements.txt. A pip
# version newer than the browsers in the base image fails at the first rendered
# fetch with "Executable doesn't exist", and that message never mentions
# versions.
ARG CRAWLER_BASE=mcr.microsoft.com/playwright/python:v1.62.0-noble
FROM ${CRAWLER_BASE}

# psycopg[binary] ships its own libpq, so there is nothing to apt-get here.
WORKDIR /srv

# Both requirement files with their folders, so that the `-r ../crawlkit/...`
# line in crawler/requirements.txt resolves the same way it does in a checkout.
COPY crawler/requirements.txt ./crawler/requirements.txt
COPY crawlkit/requirements.txt ./crawlkit/requirements.txt
RUN pip install --no-cache-dir -r crawler/requirements.txt

COPY crawlkit ./crawlkit
COPY crawler/app ./app

# /srv on the path so `import crawlkit` resolves next to `app`, exactly as it
# does from the API_V1 folder during development.
ENV PYTHONPATH=/srv

# Unbuffered, so `docker compose logs -f` shows what is happening as it happens
# rather than in blocks whenever the buffer fills.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Runs as a non-root user: this container holds API keys and a database
# password in its environment and fetches pages from the internet, and has no
# reason to be able to write anywhere. The Playwright base ships its browsers
# world-readable, so a fresh user can still start Chromium.
RUN useradd --create-home --uid 10001 crawler && chown -R crawler /srv
USER crawler

CMD ["python", "-m", "app.main"]
