# Built from the API_V1 folder, not from this folder:
#
#     docker compose build        # the compose file sets context: ../..
#
# because the image carries two folders - dashboard/standard/ (this service)
# and crawlkit/ (the crawl rules and engines shared with ../../crawler). The
# dashboard runs the "test this configuration" crawl itself, with literally
# the same code the crawler runs later, which is the only way the two cannot
# drift apart.
#
# No docker.io prefix on purpose: this image lives on Microsoft's registry,
# and the fully qualified name is what makes it work on Podman as well as
# Docker. The Playwright base is about 4 GB because it ships Chromium; that is
# what "Rendered" mode on the Sources page needs. If you never want rendered
# crawling from the dashboard, build on plain Python instead and the page will
# say "Rendered mode is not available in this dashboard build":
#
#     docker compose build --build-arg DASHBOARD_BASE=docker.io/python:3.12-slim
#
# THE TAG MUST MATCH THE PLAYWRIGHT PIN in crawlkit/requirements.txt. The pip
# package drives browser binaries that ship in this image, and a pip version
# newer than the base image fails at the first rendered fetch with "Executable
# doesn't exist" - a message that never mentions versions, in a page that
# simply reports a failed test crawl. Raise one, raise the other: the pin is
# in crawlkit/requirements.txt, the tag here and in crawler/Dockerfile.
ARG DASHBOARD_BASE=mcr.microsoft.com/playwright/python:v1.62.0-noble
FROM ${DASHBOARD_BASE}

# psycopg[binary] ships its own libpq, so there is nothing to apt-get here.
WORKDIR /srv

# Both requirement files with their folders, so that the `-r ../../crawlkit/...`
# line in dashboard/standard/requirements.txt resolves the way it does in a
# checkout. One pip command, one pin list for the crawl code the crawler also
# runs.
COPY dashboard/standard/requirements.txt ./dashboard/standard/requirements.txt
COPY crawlkit/requirements.txt ./crawlkit/requirements.txt
RUN pip install --no-cache-dir -r dashboard/standard/requirements.txt

COPY crawlkit ./crawlkit
COPY dashboard/standard/app ./app
COPY dashboard/standard/sql ./sql

# /srv on the path so `import crawlkit` resolves next to `app`, exactly as it
# does from the API_V1 folder during development.
ENV PYTHONPATH=/srv

# Unbuffered, so `docker compose logs -f` shows what is happening as it happens
# rather than in blocks whenever the buffer fills.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Runs as a non-root user: this container holds a database password in its
# environment and fetches pages from the internet on request, and has no
# reason to be able to write anywhere. The Playwright base ships its browsers
# world-readable, so a fresh user can still start Chromium.
RUN useradd --create-home --uid 10001 dashboard && chown -R dashboard /srv
USER dashboard

EXPOSE 8088
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
