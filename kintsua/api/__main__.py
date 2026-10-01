"""``python -m kintsua.api``: serve the API with uvicorn on ``0.0.0.0:$PORT`` (default 8000)."""

from __future__ import annotations

import os

import uvicorn

from kintsua.core.config import get_settings
from kintsua.log import setup_logging


def main() -> int:
    setup_logging()
    settings = get_settings()
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(
        "kintsua.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
