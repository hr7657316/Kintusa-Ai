"""Logging setup shared by every process."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging(level: str | None = None) -> None:
    lvl = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "urllib3", "github.Requester", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
