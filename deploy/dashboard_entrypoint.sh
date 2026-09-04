#!/usr/bin/env sh
# Dashboard container entrypoint: make the database true before serving a page
# that reads it.
#
# Two things have to be settled before the first request, because the first
# request is otherwise what discovers them:
#
#   schema — /api/state's first query hits `orch_runs`. Against a database that
#            has never been migrated that returns a 500 quoting a MySQL error,
#            which reads as a broken deploy rather than an empty database.
#            Nothing else on the cloud path creates those tables: the scheduler
#            does it on its first weekly run, up to a week away.
#
#   history — every panel on that page reads the platform tables. A fresh
#            instance renders all of them perfectly and says nothing ever
#            happened. The laptop holds the only copy of that history and cannot
#            reach this database, so it left a seed in the bucket; this fills
#            empty tables from it and never overwrites a table the cloud has
#            written to.
#
# Both steps are idempotent and neither is fatal. A dashboard that starts and
# reports a database problem on its own status line is more useful than a
# container that refuses to start and leaves the operator reading Docker logs.
set -eu

python3 - <<'PY' || echo "[entrypoint] 数据库准备未完成——页面会照实显示原因"
from pathlib import Path
import subprocess, sys

from ideagen import platform as plat, schema

p = plat.load()
dialect = getattr(p.state, "dialect", "")
if dialect not in ("mysql", "postgres"):
    print(f"[entrypoint] state engine is {dialect or 'local'}; nothing to do")
    raise SystemExit(0)

print(f"[entrypoint] schema migrate engine={dialect} …")
schema.migrate(p.state)

seed = Path("/app/data/platform_state.db")
if not seed.exists():
    try:
        seed.parent.mkdir(parents=True, exist_ok=True)
        seed.write_bytes(p.blobs.get("seed/platform_state.db"))
        print(f"[entrypoint] seed fetched ({seed.stat().st_size // 1024}KB)")
    except Exception as e:  # noqa: BLE001 — a missing seed is a normal state
        print(f"[entrypoint] no seed in the bucket ({type(e).__name__}); "
              "the cloud will fill these tables as it runs")

if seed.exists():
    subprocess.run([sys.executable, "/app/scripts/seed_cloud_state.py",
                    "import", "--db", str(seed)], check=False)
PY

exec python3 -m ideagen.cli serve "$@"
