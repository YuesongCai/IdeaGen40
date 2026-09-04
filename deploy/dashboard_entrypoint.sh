#!/usr/bin/env sh
# Dashboard container entrypoint: own the schema before serving a page that
# depends on it.
#
# The dashboard's only data source is /api/state, and its first query hits
# `orch_runs`. Against a database that has never been migrated that comes back
# as `Table 'ideagen.orch_runs' doesn't exist` — a 500 quoting a MySQL error,
# which reads as a broken deploy rather than as an empty database. Nothing else
# in the cloud path applies the DDL: the scheduler does it when it first runs,
# which on a fresh instance is up to a week away.
#
# `schema.migrate` is idempotent (CREATE TABLE IF NOT EXISTS plus a verify), so
# running it on every container start costs one round trip and removes a whole
# class of "the deploy looks up but the page is broken" state.
#
# It is deliberately not fatal. A dashboard that starts and reports a database
# problem on its own status line is more useful than a container that refuses to
# start and leaves the operator reading Docker logs to find out why.
set -eu

python3 - <<'PY' || echo "[entrypoint] schema migrate skipped — the page will report the reason"
from ideagen import platform as plat, schema
p = plat.load()
dialect = getattr(p.state, "dialect", "")
if dialect in ("mysql", "postgres"):
    n = schema.migrate(p.state)
    print(f"[entrypoint] schema ok engine={dialect} statements={n}")
else:
    print(f"[entrypoint] state engine is {dialect or 'local'}; nothing to migrate")
PY

exec python3 -m ideagen.cli serve "$@"
