"""HTTP helpers shared by the OpenAPI, changelog and package watchers.

* ``net_guarded(meta)`` / ``guard_url(url, meta)`` / ``client_kwargs(meta)``: the hosted
  worker marks every source that is not operator-seeded with ``meta["_net_guard"] = True``
  (``kintsua.watchers.run.source_ref_for``). Such a fetch checks the URL with
  ``kintsua.core.netguard.check_url`` before the first request and installs
  ``async_request_hook`` so every redirect hop is checked too. The CLI never sets the flag,
  so ``kintsua watch`` can still reach a developer's localhost server.
* ``get_bounded(client, url, max_bytes=...)``: a streamed GET that aborts once the body
  passes a byte cap (``meta["max_bytes"]``, default 32 MB), so a hostile or broken source
  cannot exhaust the worker's memory.

Base dependencies only (httpx, stdlib), so the open-source watchers may import it.
"""

from __future__ import annotations

from typing import Any

import httpx

from kintsua.core.netguard import async_request_hook, check_url

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_DROP_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


class ResponseTooLarge(ValueError):
    """The response body is larger than the configured cap."""


def net_guarded(meta: dict[str, Any] | None) -> bool:
    return bool((meta or {}).get("_net_guard"))


def guard_url(url: str, meta: dict[str, Any] | None) -> None:
    """Raise ``kintsua.core.netguard.BlockedURL`` when the source is guarded and ``url``
    is not a public http(s) destination."""
    if net_guarded(meta):
        check_url(url)


def client_kwargs(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Extra ``httpx.AsyncClient`` kwargs: the redirect-checking request hook when guarded."""
    if net_guarded(meta):
        return {"event_hooks": {"request": [async_request_hook]}}
    return {}


def max_bytes_for(meta: dict[str, Any] | None) -> int:
    try:
        value = int((meta or {}).get("max_bytes") or MAX_RESPONSE_BYTES)
    except (TypeError, ValueError):
        value = MAX_RESPONSE_BYTES
    return max(1, value)


async def get_bounded(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET ``url`` streaming the body and raise ``ResponseTooLarge`` past ``max_bytes``.

    Returns an ordinary (already read) ``httpx.Response`` for the final hop, so callers keep
    using ``status_code`` / ``raise_for_status()`` / ``text`` / ``json()``."""
    async with client.stream("GET", url, headers=headers) as r:
        declared = r.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge(
                f"{url}: response of {int(declared)} bytes exceeds the {max_bytes}-byte limit"
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge(f"{url}: response exceeds the {max_bytes}-byte limit")
            chunks.append(chunk)
        status, request = r.status_code, r.request
        kept = [(k, v) for k, v in r.headers.multi_items() if k.lower() not in _DROP_HEADERS]
    # the body is already decoded: drop the encoding/length headers of the wire response
    return httpx.Response(status, headers=kept, content=b"".join(chunks), request=request)
