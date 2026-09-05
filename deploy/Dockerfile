# Run image for the unattended scheduler.
#
# NO CREDENTIALS IN THIS IMAGE, AT ANY LAYER. Not as ARG, not as ENV, not as a
# copied .env file. Two reasons, and the second is the one people forget:
# an image layer is readable by anyone who can pull the image, and `docker
# history` keeps build args even when a later layer deletes the file. The process
# reads secrets at runtime only — from the environment injected by the sandbox /
# workload definition, which `KmsSecretStore` prefers to resolve through BytePlus
# KMS and falls back to the environment for (that fallback is reported by
# `platform doctor`, so a misconfigured KMS cannot hide).
#
# `~/.ideagen.env` deliberately does not exist here: it is the laptop convenience
# path, and a file inside a container is exactly the thing this image must not
# have.
FROM python:3.12-slim

# The pipeline is timezone-critical: the scheduler converts a UTC instant to a
# Hong Kong date, and zoneinfo needs the tz database present. python:*-slim does
# not ship one, so without tzdata `ZoneInfo("Asia/Hong_Kong")` raises and the run
# never starts. Install it explicitly rather than relying on the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Container clock stays UTC on purpose. The HKT conversion belongs to one place
# in the code (`ideagen/scheduler.py`), and a container whose TZ is Asia/Hong_Kong
# would make `datetime.now()` look correct while hiding the bug the day something
# runs somewhere else.
ENV TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code change does not re-resolve the wheel set.
#
# The cloud SDKs are installed here rather than in requirements.txt because the
# laptop does not need them and the adapters import them lazily — that laziness is
# what lets `platform doctor` report "the TOS SDK is missing" instead of failing
# to import. futu-api is intentionally NOT installed: OpenD is a local gateway
# that a sandbox cannot reach, so price sync degrades to stored bars (see the
# runbook) and pulling in pandas would double the image for nothing.
RUN pip install --no-cache-dir \
      "requests>=2.31" \
      "tos" \
      "psycopg[binary]" \
      "PyMySQL[rsa]>=1.1" \
      "redis" \
      "kafka-python" \
      "openai" \
      "volcengine" \
      "pytest"

COPY ideagen ./ideagen
# The instance has no shell, so anything an operator would have run by hand has
# to be inside the image for the entrypoints to call — seeding the database from
# the laptop's history is the current one.
COPY scripts ./scripts
# The updater runs this suite against a freshly built image before it
# replaces the running one. Without the tests in the image there is no
# gate between `git push` and production, and several agents push here.
COPY tests ./tests
COPY prompts ./prompts
COPY seed ./seed
COPY web ./web
COPY deploy/entrypoint.sh ./deploy/entrypoint.sh
COPY deploy/dashboard_entrypoint.sh ./deploy/dashboard_entrypoint.sh
RUN chmod +x ./deploy/entrypoint.sh ./deploy/dashboard_entrypoint.sh

# State and artifacts belong in the platform ports, not on this filesystem: the
# sandbox is discarded after the run. This directory exists only for the local
# SQLite fallback during the first migration step, and losing it must never lose
# anything that matters — which is exactly what the configured RDS state port
# and `IDEAGEN_TOS_BUCKET` are for.
# `/var/lib/ideagen-accounts` is where a compose deployment keeps the account
# list, on a named volume. It is created here, owned by the runtime user, for a
# specific docker behaviour: a *fresh* named volume inherits the ownership and
# mode of the image directory it is mounted over. Without this line docker
# creates it root-owned 0755, the unprivileged container cannot write it, and
# the accounts silently fall back into the container — which is the exact
# failure this volume exists to prevent, arriving in the disguise of a fix.
RUN mkdir -p /app/data /var/lib/ideagen-accounts \
    && useradd -u 10001 -m ideagen \
    && chown -R ideagen /app /var/lib/ideagen-accounts
USER ideagen

ENV IDEAGEN_PLATFORM=byteplus \
    IDEAGEN_VENUE=paper \
    IDEAGEN_TICK_INTERVAL_S=300

# A container that has stopped ticking must not look healthy. The probe asks the
# scheduler's own health surface, which reads the heartbeat key rather than
# asserting anything about itself.
HEALTHCHECK --interval=120s --timeout=30s --start-period=60s --retries=3 \
  CMD python3 -c "import sys,json;from datetime import datetime,timezone;\
from ideagen import scheduler as s;\
h=s.health(datetime.now(timezone.utc));sys.exit(0 if h['alive'] else 1)"

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
