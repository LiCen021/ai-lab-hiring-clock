"""Shared paths, config loading and raw-file I/O."""
from __future__ import annotations

import gzip
import logging
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
SITE = ROOT / "site"

log = logging.getLogger("hiring_clock")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


@lru_cache
def settings() -> dict:
    return yaml.safe_load((CONFIG / "settings.yaml").read_text())


def maybe_gunzip(data: bytes) -> bytes:
    """Wayback `id_` responses are sometimes the original gzip-encoded bytes."""
    while data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def write_gz(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wb") as f:
        f.write(maybe_gunzip(data))
    tmp.replace(path)


def read_gz_text(path: Path) -> str:
    with gzip.open(path, "rb") as f:
        return maybe_gunzip(f.read()).decode("utf-8", errors="replace")
