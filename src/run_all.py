"""Pipeline orchestrator: python -m src.run_all [--phase backfill|live]

Stages are added here as they are built (ARCHITECTURE §9). Each stage is idempotent and
reads cached outputs from the previous one.
"""
from __future__ import annotations

import argparse

from . import dedupe, fetch_wayback, snapshots
from .common import log, setup_logging


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--phase", choices=["backfill", "live"], default="backfill")
    p.add_argument("--skip-fetch", action="store_true", help="parse only what is already cached")
    a = p.parse_args()
    setup_logging()
    if a.phase == "live":
        raise SystemExit("live phase not built yet (build step 8)")
    if not a.skip_fetch:
        fetch_wayback.run()
    parsed = snapshots.run()
    dedupe.run(parsed["rows"], parsed["listings"], parsed["details"])
    log.info("pipeline complete up to: dedupe (tagging onward not built yet)")


if __name__ == "__main__":
    main()
