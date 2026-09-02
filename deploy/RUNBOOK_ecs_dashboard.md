# ECS Dashboard Deployment

This runbook intentionally contains no account IDs, instance IDs, addresses,
bucket names, presigned URLs, image digests, or credentials.

## Prerequisites

- An x86_64 Linux host with Docker and the Compose plugin.
- A private MySQL-compatible database.
- An object-storage bucket.
- A public HTTPS hostname or address.
- Runtime credentials stored outside the repository.

## Build

```bash
git status --short
SHA=$(git rev-parse --short HEAD)
docker build --pull=never -t ideagen40:${SHA} -f deploy/Dockerfile .
docker run --rm --entrypoint python3 ideagen40:${SHA} \
  -m ideagen.cli poc-load-public-mock --verify-only
```

## Runtime Configuration

Create `/opt/ideagen/config/runtime.env` with mode `0600`. Start from
`.env.example` and populate values through the deployment owner's secret
management process. Do not paste credentials into shell history, image build
arguments, object-storage URLs, or this document.

Create the OAuth token directory separately:

```bash
sudo install -d -m 700 -o 10001 -g 10001 /opt/ideagen/oauth
```

Set non-secret release parameters and start the services:

```bash
export IMAGE_TAG=<git-short-sha>
export IDEAGEN_PUBLIC_SITE=https://<dashboard-host>
export IDEAGEN_DEFAULT_SNI=<dashboard-host>
cd <release-directory>/deploy
docker compose -p ideagen up -d scheduler dashboard proxy
docker compose -p ideagen ps
```

## Verification

```bash
docker compose -p ideagen exec -T dashboard \
  python3 -c "import urllib.request; print(urllib.request.urlopen(
  'http://127.0.0.1:8765/healthz', timeout=3).status)"
curl -fsS https://<dashboard-host>/healthz
```

Expected behavior:

- `/healthz` returns HTTP 200.
- An unauthenticated dashboard request returns HTTP 401.
- The business port is not exposed directly to the internet.
- Application state survives container replacement because it resides in the
  configured database and object store.

## Upgrade And Rollback

Build each release with an immutable tag. To upgrade, set `IMAGE_TAG` to the new
tag and recreate the services. To roll back, restore the previous tag and run:

```bash
docker compose -p ideagen up -d --force-recreate
```
