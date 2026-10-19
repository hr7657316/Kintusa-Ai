"""Shared test setup for the open-source CLI and SDK: no network, no hosted service, no database."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("KINTSUA_ENV", "test")
os.environ.setdefault("DATA_DIR", str(Path(__file__).parent / ".data"))

FIXTURES = Path(__file__).parent / "fixtures"
