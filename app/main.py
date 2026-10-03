"""FastAPI application factory, middleware, router mounting and the SPA index route."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.deps import csrf_is_valid
from app.rate_limit import limiter
from app.security import generate_csrf_token, validate_csrf_token
from app.services.jobs import sweep_stuck_jobs
from app.services.storage import StorageUnavailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Login and registration are the only CSRF-exempt mutating routes: no session exists yet to
# ride on, and both are rate limited. Everything else needs the double-submit token.
CSRF_EXEMPT_PATHS = {"/api/auth/login", "/api/auth/register"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
JSON_BODY_LIMIT = 2 * 1024 * 1024
UPLOAD_OVERHEAD = 1024 * 1024


class BodySizeLimitMiddleware:
    """Reject request bodies over the limit before the application reads them (HTTP 413).

    JSON bodies are capped at 2 MB; multipart uploads at the largest upload limit plus 1 MB of
    form overhead. The check uses Content-Length when present and counts streamed bytes otherwise.
    """

    def __init__(self, app, upload_limit: int):
        self.app = app
        self.upload_limit = upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        is_upload = headers.get("content-type", "").startswith("multipart/")
        limit = self.upload_limit if is_upload else JSON_BODY_LIMIT

        declared = headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await self._reject(send)
            return

        received = 0
        too_large = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        started = False

        async def guarded_send(message):
            nonlocal started
            if too_large and not started:
                started = True
                await self._reject(send)
                return
            if too_large:
                return
            await send(message)

        await self.app(scope, limited_receive, guarded_send)

    @staticmethod
    async def _reject(send) -> None:
        body = b'{"detail":"Request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def content_security_policy(voice_enabled: bool) -> str:
    """The spec's CSP. The ElevenLabs agent websocket is allowed only when the voice agent is on."""
    connect = (
        "'self' wss://api.elevenlabs.io https://api.elevenlabs.io" if voice_enabled else "'self'"
    )
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data: blob: https://*.amazonaws.com; "
        "media-src 'self' blob: https://*.amazonaws.com; "
        f"connect-src {connect}; "
        "object-src 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown."""
    logger.info("ClinicalScribe starting up")
    swept = sweep_stuck_jobs()
    if swept:
        logger.info("Swept %d stuck background jobs on startup", swept)
    yield
    logger.info("ClinicalScribe shutting down")


def create_app() -> FastAPI:
    settings = get_settings()
    voice_enabled = settings.VOICE_AGENT_PROVIDER != "none"

    app = FastAPI(
        title="ClinicalScribe",
        description="Clinical consultation documentation and prescription management platform",
        version="1.0.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )
    app.state.limiter = limiter

    # ── Error handlers: consistent {"detail": ...} shape, no leaked internals ──────────
    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please slow down and try again shortly."},
            headers={"Retry-After": "60"},
        )

    @app.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, exc: StorageUnavailable):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Never echo the submitted values (they can include passwords).
        problems = []
        for error in exc.errors():
            field = ".".join(
                str(part) for part in error.get("loc", ()) if part not in ("body", "query", "path")
            )
            message = str(error.get("msg", "Invalid value")).removeprefix("Value error, ")
            problems.append(f"{field}: {message}" if field else message)
        return JSONResponse(
            status_code=422, content={"detail": "; ".join(problems) or "Invalid request"}
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500, content={"detail": "Something went wrong. Please try again."}
        )

    # ── Middleware (outermost last): headers, CSRF, body size ─────────────────────────────
    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Permissions-Policy"] = "microphone=(self)"
            response.headers["Content-Security-Policy"] = content_security_policy(voice_enabled)
            if settings.is_production:
                response.headers["Strict-Transport-Security"] = (
                    "max-age=31536000; includeSubDomains"
                )
            if request.url.path.startswith("/api/"):
                response.headers.setdefault("Cache-Control", "no-store")
            return response

    class CSRFMiddleware(BaseHTTPMiddleware):
        """Require the signed double-submit token on every state-changing request."""

        async def dispatch(self, request: Request, call_next):
            if request.method in SAFE_METHODS or request.url.path in CSRF_EXEMPT_PATHS:
                return await call_next(request)
            if not csrf_is_valid(
                request.cookies.get("csrf_token"), request.headers.get("X-CSRF-Token")
            ):
                return JSONResponse(
                    status_code=403, content={"detail": "CSRF token missing or invalid"}
                )
            return await call_next(request)

    # add_middleware stacks outward: security headers wrap everything (including 403/413 replies).
    app.add_middleware(
        BodySizeLimitMiddleware,
        upload_limit=max(settings.max_audio_bytes, 10 * 1024 * 1024) + UPLOAD_OVERHEAD,
    )
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    from app.routers import (
        admin,
        appointments,
        auth,
        availability,
        consents,
        consults,
        doctor,
        files,
        patient,
        prescriptions,
        reports,
        voice,
    )

    for module in (
        auth,
        patient,
        doctor,
        admin,
        appointments,
        availability,
        consents,
        consults,
        prescriptions,
        files,
        reports,
        voice,
    ):
        app.include_router(module.router)

    @app.get("/healthz")
    def healthz():
        """Liveness plus database connectivity. No auth."""
        try:
            from sqlalchemy import text

            from app.db import engine

            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception:
            logger.exception("Health check could not reach the database")
            return JSONResponse(status_code=503, content={"status": "error"})

    @app.get("/", response_class=HTMLResponse)
    @app.get("/{path:path}", response_class=HTMLResponse)
    def index(request: Request, path: str = ""):
        """Serve the single-page shell; unknown API paths get a JSON 404 instead."""
        if path.startswith(("api/", "static/")) or path in {
            "api",
            "static",
            "healthz",
            "docs",
            "openapi.json",
        }:
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        csrf_token = request.cookies.get("csrf_token", "")
        if not validate_csrf_token(csrf_token):
            csrf_token = generate_csrf_token()
        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "app_name": "ClinicalScribe",
                "csrf_token": csrf_token,
                "env": settings.ENV,
                "voice_enabled": "true" if voice_enabled else "false",
                "max_audio_mb": settings.MAX_AUDIO_MB,
            },
        )
        response.set_cookie(
            key="csrf_token",
            value=csrf_token,
            httponly=False,  # the frontend reads it to send X-CSRF-Token
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()
