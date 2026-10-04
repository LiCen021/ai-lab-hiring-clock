"""Pipeline orchestrator: python -m src.run_all [--phase backfill|live]

Stages are added here as they are built (ARCHITECTURE §9). Each stage is idempotent and
reads cached outputs from the previous one.
"""
from __future__ import annotations

import argparse

from . import fetch_wayback
from .common import log, setup_logging


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--phase", choices=["backfill", "live"], default="backfill")
    a = p.parse_args()
    setup_logging()
    if a.phase == "live":
        raise SystemExit("live phase not built yet (build step 8)")
    fetch_wayback.run()
    log.info("pipeline complete up to: fetch (parse onward pending review of build step 1)")


if __name__ == "__main__":
    main()
