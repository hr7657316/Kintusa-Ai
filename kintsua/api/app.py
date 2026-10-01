"""The FastAPI application: ``create_app()`` mounts every router, the static files and the
Jinja2 templates, adds ``/healthz`` (pings Postgres), structured error handlers that never
leak a stack trace, and request logging with a request id.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from kintsua.api import auth, billing, ingest, keys, pages, report, repos, setup, webhooks
from kintsua.api.deps import STATIC_DIR, is_htmx, render
from kintsua.core.config import get_settings

log = logging.getLogger(__name__)

API_PREFIXES = ("/v1", "/webhooks", "/healthz", "/api")


def _wants_json(request: Request) -> bool:
    if is_htmx(request):
        return False
    if request.url.path.startswith(API_PREFIXES):
        return True
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    # a client that sends JSON is a JSON client, whatever it accepts
    return (
        "application/json" in request.headers.get("content-type", "") and "text/html" not in accept
    )


def _error_response(request: Request, status: int, title: str, detail: Any) -> Response:
    message = detail if isinstance(detail, str) else title
    if _wants_json(request):
        body: dict[str, Any] = {"error": message, "status": status}
        if not isinstance(detail, str) and detail is not None:
            body["details"] = detail
        return JSONResponse(body, status_code=status)
    if is_htmx(request):
        return render(
            request, "partials/notice.html", status_code=status, kind="error", message=message
        )
    return render(request, "error.html", status_code=status, title=title, message=message)


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for err in exc.errors():
        if not isinstance(err, dict):
            out.append({"msg": str(err)})
            continue
        out.append(
            {
                "loc": [str(x) for x in err.get("loc", ())],
                "msg": str(err.get("msg", "invalid value")),
                "type": str(err.get("type", "")),
            }
        )
    return out


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Kintsua",
        version="0.1.0",
        description="Self-maintaining integrations for AI agents.",
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env != "prod" else None,
    )
    app.state.settings = settings

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for r in (
        webhooks.router,
        ingest.router,
        repos.router,
        keys.router,
        billing.router,
        report.router,
        auth.router,
        setup.router,
        pages.router,
    ):
        app.include_router(r)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> Response:
        from kintsua.core.db import ping

        try:
            ping()
        except Exception as e:  # noqa: BLE001 - report, do not raise
            log.warning("healthz: database ping failed: %s", type(e).__name__)
            return JSONResponse({"ok": False, "db": False, "env": settings.env}, status_code=503)
        return JSONResponse({"ok": True, "db": True, "env": settings.env})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        resp = _error_response(request, exc.status_code, _title_for(exc.status_code), exc.detail)
        for k, v in (getattr(exc, "headers", None) or {}).items():
            resp.headers[k] = v
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        details = _validation_details(exc)
        if _wants_json(request):
            return JSONResponse(
                {"error": "validation failed", "status": 422, "details": details}, status_code=422
            )
        message = "; ".join(
            f"{'.'.join(d.get('loc', [])) or 'body'}: {d.get('msg')}" for d in details
        )
        return _error_response(request, 422, "Invalid request", message or "validation failed")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        rid = getattr(request.state, "request_id", "-")
        log.exception(
            "unhandled error on %s %s (request %s)", request.method, request.url.path, rid
        )
        resp = _error_response(request, 500, "Internal error", f"internal error (request {rid})")
        resp.headers["X-Request-Id"] = str(rid)
        return resp

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.info(
                "%s %s -> 500 %.1fms rid=%s",
                request.method,
                request.url.path,
                (time.perf_counter() - t0) * 1000,
                rid,
            )
            raise
        ms = (time.perf_counter() - t0) * 1000
        response.headers.setdefault("X-Request-Id", rid)
        if request.url.path != "/healthz":
            log.info(
                "%s %s -> %s %.1fms rid=%s",
                request.method,
                request.url.path,
                response.status_code,
                ms,
                rid,
            )
        return response

    return app


def _title_for(status: int) -> str:
    return {
        400: "Bad request",
        401: "Sign in required",
        403: "Forbidden",
        404: "Not found",
        405: "Method not allowed",
        409: "Conflict",
        413: "Payload too large",
        422: "Invalid request",
        429: "Too many requests",
        500: "Internal error",
        502: "Upstream error",
        503: "Service unavailable",
    }.get(status, f"HTTP {status}")


__all__ = ["create_app"]
