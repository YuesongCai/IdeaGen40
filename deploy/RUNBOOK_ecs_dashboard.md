# ECS Dashboard Deployment

This runbook intentionally contains no account IDs, instance IDs, addresses,
bucket names, presigned URLs, image digests, or credentials.

## Prerequisites

- An x86_64 Linux host with Docker and the Compose plugin.
- A private MySQL-compatible database.
- An object-storage bucket.
- A public HTTPS hostname or address.
- Runtime credentials stored outside the repository.

## Bootstrapping Without A Shell

Use this when neither SSH nor the platform's command agent is available — for
example when the operator network filters SSH to every destination and the
instance image fails to register a command agent. cloud-init `UserData` is then
the only execution path, and it runs on every boot, which makes a reboot the
deploy.

`deploy/instance_bootstrap.sh` is the source of truth for what the instance does
at boot: install Docker, fetch `origin/main`, build the image, and start the
stack. It contains no credentials, because UserData is stored in clear text by
the cloud API and is readable from the console. Until the runtime configuration
exists on the instance, the script starts nothing and says so on a status page —
a stack that came up half-configured would be worse than an honest "waiting".

```bash
python3 scripts/deploy_userdata.py push     # ship the bootstrap
python3 scripts/deploy_userdata.py secrets  # + one short-lived config URL
python3 scripts/deploy_userdata.py reboot   # run it, then watch
python3 scripts/deploy_userdata.py forget   # delete the temporary object
```

`secrets` and `reboot` are separate verbs on purpose: shipping a file must not
restart production as a side effect. The status page on port 80 is what tells
"still installing" apart from "unreachable" — with no shell those two look
identical from outside. Once the stack is up the proxy owns that port and
`/healthz` answers instead, so which of the two replies is itself the state.

The bootstrap also re-attempts the command-agent install on every boot. Once
that succeeds, `scripts/deploy_cloud.py` takes over and this path is only
needed again for a cold start.

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
