"""``python -m kintsua.watchers`` is the daily watcher run (same as ``python -m
kintsua.watchers.run``)."""

from __future__ import annotations

from kintsua.watchers.run import main

if __name__ == "__main__":
    raise SystemExit(main())
