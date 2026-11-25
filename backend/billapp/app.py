"""FastAPI Application Factory, Lifespan Lifecycle, Middleware & Error Handlers."""

from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Generator, Optional, Union
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from billapp.classify.categories import CategoryService
from billapp.core.db import create_connection, get_default_db_path, init_db
from billapp.version import APP_VERSION

logger = logging.getLogger("billapp.app")


# =========================================================================
# Application Exception Hierarchy
# =========================================================================

class AppException(Exception):
    """Base application exception mapped to semantic JSON error responses."""
    def __init__(
        self,
        status_code: int = 400,
        code: str = "BAD_REQUEST",
        message: str = "Bad Request",
        details: Optional[Any] = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class NotFoundError(AppException):
    def __init__(self, message: str = "资源不存在", details: Optional[Any] = None):
        super().__init__(status_code=404, code="NOT_FOUND", message=message, details=details)


class ValidationError(AppException):
    def __init__(self, message: str = "请求参数校验失败", details: Optional[Any] = None):
        super().__init__(status_code=422, code="VALIDATION_ERROR", message=message, details=details)


class ConflictError(AppException):
    def __init__(self, message: str = "资源冲突", details: Optional[Any] = None):
        super().__init__(status_code=409, code="CONFLICT", message=message, details=details)


class CategoryInUseError(AppException):
    def __init__(self, category_id: int, count: int):
        super().__init__(
            status_code=409,
            code="CATEGORY_IN_USE",
            message=f"该分类下仍有 {count} 条交易记录，无法直接删除",
            details={"category_id": category_id, "transaction_count": count},
        )


# =========================================================================
# Database Connection Dependency
# =========================================================================

def get_db_conn(request: Request) -> Generator[sqlite3.Connection, None, None]:
    """Request-scoped raw SQLite connection dependency for FastAPI.
    
    Reads db_path from request.app.state.db_path and manages commit/rollback/close.
    """
    db_path = getattr(request.app.state, "db_path", None)
    conn = create_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =========================================================================
# Builtin Seed Helpers
# =========================================================================

LEGACY_BUILTIN_PROFILE_IDS = ("ccb_debit", "cmb_debit", "pab_credit", "cmb_pdf")


def _cleanup_legacy_builtin_profiles(conn: sqlite3.Connection) -> None:
    """Remove legacy unverified built-in bank profile templates while preserving user profiles."""
    placeholders = ",".join("?" for _ in LEGACY_BUILTIN_PROFILE_IDS)
    conn.execute(
        f"DELETE FROM bank_profiles WHERE is_builtin = 1 AND id IN ({placeholders})",
        LEGACY_BUILTIN_PROFILE_IDS,
    )
    conn.commit()


# =========================================================================
# Lifespan Lifecycle
# =========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize schema, seed taxonomy on startup; checkpoint on shutdown."""
    db_path = getattr(app.state, "db_path", None)
    logger.info("BillApp lifespan starting (version=%s)", APP_VERSION)
    init_conn = init_db(db_path)
    try:
        CategoryService.init_builtin_categories(init_conn)
        _cleanup_legacy_builtin_profiles(init_conn)
        init_conn.commit()
    finally:
        init_conn.close()

    yield

    # Clean shutdown checkpoint
    logger.info("BillApp lifespan shutting down; running SQLite WAL checkpoint.")
    try:
        cleanup_conn = create_connection(db_path)
        cleanup_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        cleanup_conn.close()
    except Exception as exc:
        logger.warning("SQLite WAL checkpoint during shutdown failed: %s", exc)


# =========================================================================
# Application Factory
# =========================================================================

def _extract_host_hostname(host_header: str) -> str:
    """Extract lowercase hostname from an HTTP Host header (supporting IPv6 brackets and ports)."""
    raw = (host_header or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end != -1:
            return raw[1:end]
        return raw
    if ":" in raw:
        return raw.rsplit(":", 1)[0]
    return raw


def _resolve_allowed_origins() -> list[str]:
    """Build explicit local CORS allowlist without wildcards."""
    origins: list[str] = []
    env_origins = os.environ.get("BILLAPP_ALLOWED_ORIGINS", "").strip()
    if env_origins:
        for item in env_origins.split(","):
            cleaned = item.strip().rstrip("/")
            if cleaned and cleaned != "*":
                origins.append(cleaned)
    if os.environ.get("BILLAPP_DEV_MODE", "").strip().lower() in ("1", "true", "yes"):
        for dev_origin in (
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ):
            if dev_origin not in origins:
                origins.append(dev_origin)
    return origins


def create_app(
    db_path: Optional[Union[str, Path]] = None,
    api_token: Optional[str] = None,
    bootstrap_token: Optional[str] = None,
    log_path: Optional[Union[str, Path]] = None,
) -> FastAPI:
    """FastAPI application factory supporting configurable SQLite database path and local token auth."""
    app = FastAPI(
        title="账单综合管理系统 API",
        version=APP_VERSION,
        description="多来源账单综合管理程序后端 REST API",
        lifespan=lifespan,
    )
    app.state.db_path = db_path if db_path is not None else get_default_db_path()
    app.state.log_path = log_path

    effective_api_token = (
        api_token if api_token is not None else (os.environ.get("BILLAPP_API_TOKEN") or "").strip() or None
    )
    effective_boot_token = (
        bootstrap_token
        if bootstrap_token is not None
        else (os.environ.get("BILLAPP_BOOTSTRAP_TOKEN") or "").strip() or None
    )
    app.state.api_token = effective_api_token
    app.state.bootstrap_token = effective_boot_token

    allowed_origins = _resolve_allowed_origins()
    allowed_origin_set = set(allowed_origins)

    # Strict CORS configuration: never allow wildcard "*"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=bool(allowed_origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-BillApp-Token", "Accept"],
        expose_headers=["Content-Disposition", "X-BillApp-Log-Filename"],
    )

    # Local security middleware: Host DNS-rebinding defense, Origin CSRF defense, Token auth & API logging
    @app.middleware("http")
    async def local_security_middleware(request: Request, call_next):
        started_at = time.perf_counter()
        token = getattr(app.state, "api_token", None)
        path = request.url.path

        # 1. Host header allowlist (blocks DNS rebinding)
        host_header = (request.headers.get("host") or "").strip()
        hostname = _extract_host_hostname(host_header)
        allowed_hosts = {"127.0.0.1", "localhost", "::1"}
        if token is None:
            allowed_hosts.add("testserver")
        if hostname not in allowed_hosts:
            logger.warning("Blocked %s %s: INVALID_HOST (host=%s)", request.method, path, hostname)
            return JSONResponse(
                status_code=403,
                content={
                    "code": "INVALID_HOST",
                    "message": "拒绝非本机 Host 请求头",
                    "details": hostname,
                },
            )

        # 2. Cross-origin check for state-changing requests
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = (request.headers.get("origin") or "").strip().rstrip("/")
            if origin:
                parsed = urlparse(origin)
                origin_norm = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" if parsed.scheme and parsed.netloc else ""
                same_origin_http = f"http://{host_header.lower()}"
                same_origin_https = f"https://{host_header.lower()}"
                if origin_norm not in allowed_origin_set and origin_norm not in (same_origin_http, same_origin_https):
                    logger.warning("Blocked %s %s: FORBIDDEN_ORIGIN (origin=%s)", request.method, path, origin)
                    return JSONResponse(
                        status_code=403,
                        content={
                            "code": "FORBIDDEN_ORIGIN",
                            "message": "已拦截跨站写操作请求",
                            "details": origin,
                        },
                    )

        # 3. Standalone browser one-time bootstrap token -> HttpOnly SameSite=Strict session cookie
        if (
            token
            and request.method == "GET"
            and path == "/"
            and "boot_token" in request.query_params
        ):
            boot_expected = getattr(app.state, "bootstrap_token", None)
            provided_boot = request.query_params.get("boot_token", "")
            if boot_expected and hmac.compare_digest(provided_boot, boot_expected):
                app.state.bootstrap_token = None
                resp = RedirectResponse(url="/", status_code=302)
                resp.set_cookie(
                    key="billapp_session",
                    value=token,
                    httponly=True,
                    samesite="strict",
                    path="/",
                )
                return resp

        # 4. Enforce local API token on /api/* (except /api/health) and OpenAPI docs
        protected = (
            (path.startswith("/api/") and path != "/api/health")
            or path in ("/docs", "/redoc", "/openapi.json")
        )
        if token and request.method != "OPTIONS" and protected:
            hdr_token = (request.headers.get("x-billapp-token") or "").strip()
            cookie_token = (request.cookies.get("billapp_session") or "").strip()
            authorized = (
                (bool(hdr_token) and hmac.compare_digest(hdr_token, token))
                or (bool(cookie_token) and hmac.compare_digest(cookie_token, token))
            )
            if not authorized:
                logger.warning("Blocked %s %s: UNAUTHORIZED (missing or invalid session token)", request.method, path)
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "UNAUTHORIZED",
                        "message": "缺少或无效的本地会话鉴权令牌",
                    },
                )

        try:
            response = await call_next(request)
        except Exception as exc:
            if path.startswith("/api/"):
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                logger.error(
                    "API %s %s -> 500 (%dms) unhandled=%s",
                    request.method,
                    path,
                    elapsed_ms,
                    type(exc).__name__,
                )
            raise
        if path.startswith("/api/") and (
            request.method in ("POST", "PUT", "PATCH", "DELETE") or response.status_code >= 400
        ):
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            if response.status_code >= 500:
                logger.error("API %s %s -> %d (%dms)", request.method, path, response.status_code, elapsed_ms)
            elif response.status_code >= 400:
                logger.warning("API %s %s -> %d (%dms)", request.method, path, response.status_code, elapsed_ms)
            else:
                logger.info("API %s %s -> %d (%dms)", request.method, path, response.status_code, elapsed_ms)
        return response

    # Custom Exception Handlers
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        if exc.status_code >= 500:
            logger.error(
                "AppException %s on %s %s: code=%s message=%s",
                exc.status_code,
                request.method,
                request.url.path,
                exc.code,
                exc.message,
            )
        elif exc.status_code >= 400:
            logger.warning(
                "AppException %s on %s %s: code=%s",
                exc.status_code,
                request.method,
                request.url.path,
                exc.code,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    def _sanitize_for_json(obj: Any) -> Any:
        if isinstance(obj, float):
            return obj if math.isfinite(obj) else str(obj)
        if isinstance(obj, dict):
            return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_sanitize_for_json(v) for v in obj]
        if isinstance(obj, (str, int, bool)) or obj is None:
            return obj
        return str(obj)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        locs = [
            ".".join(str(part) for part in err.get("loc", ()))
            for err in (exc.errors() or [])[:5]
        ]
        logger.warning(
            "ValidationError 422 on %s %s: fields=%s",
            request.method,
            request.url.path,
            locs,
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "字段校验错误",
                "details": _sanitize_for_json(exc.errors()),
            },
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def sqlite_integrity_handler(request: Request, exc: sqlite3.IntegrityError):
        err_msg = str(exc)
        status_code = 409 if "unique" in err_msg.lower() else 400
        logger.warning("SQLite IntegrityError %d on %s %s", status_code, request.method, request.url.path)
        return JSONResponse(
            status_code=status_code,
            content={"code": "INTEGRITY_ERROR", "message": "数据完整性约束校验失败", "details": err_msg},
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_busy_handler(request: Request, exc: sqlite3.OperationalError):
        err_msg = str(exc).lower()
        logger.error("SQLite OperationalError on %s %s: %s", request.method, request.url.path, exc)
        if "locked" in err_msg or "busy" in err_msg:
            return JSONResponse(
                status_code=503,
                content={"code": "DB_BUSY", "message": "数据库被占用，请稍后重试", "details": str(exc)},
            )
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL", "message": "数据库操作异常", "details": str(exc)},
        )

    # System Health and Meta Endpoints
    @app.get("/api/health", tags=["system"])
    async def health_check():
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/api/meta", tags=["system"])
    async def get_system_meta(db: sqlite3.Connection = Depends(get_db_conn)):
        pending_review_count = db.execute(
            """
            SELECT COUNT(*)
            FROM match_links ml
            JOIN transactions t1 ON ml.primary_id = t1.id
            JOIN transactions t2 ON ml.secondary_id = t2.id
            WHERE ml.status = 'pending'
              AND (
                (ml.kind = 'duplicate' AND t1.duplicate_of IS NULL AND t2.duplicate_of IS NULL)
                OR (ml.kind = 'transfer' AND t1.transfer_peer IS NULL AND t2.transfer_peer IS NULL)
              )
            """
        ).fetchone()[0]
        categories_tree = CategoryService.get_tree(db)
        accounts_rows = db.execute("SELECT id, kind, bank, tail, alias FROM accounts").fetchall()
        accounts_list = [dict(r) for r in accounts_rows]

        return {
            "version": APP_VERSION,
            "categories": [c.model_dump() for c in categories_tree],
            "accounts": accounts_list,
            "pending_review_count": pending_review_count,
            "db_path": str(getattr(app.state, "db_path", None) or get_default_db_path()),
        }

    # Register API Route Blueprints
    from billapp.api.import_ import router as import_router
    from billapp.api.profiles import router as profiles_router
    from billapp.api.transactions import router as transactions_router
    from billapp.api.review import router as review_router
    from billapp.api.categories import router as categories_router
    from billapp.api.rules import router as rules_router
    from billapp.api.llm import router as llm_router
    from billapp.api.settings import router as settings_router, data_router
    from billapp.api.accounts import router as accounts_router
    from billapp.api.stats import router as stats_router
    from billapp.api.export import router as export_router
    from billapp.api.fx import router as fx_router
    from billapp.api.logs import router as logs_router

    app.include_router(import_router)
    app.include_router(profiles_router)
    app.include_router(transactions_router)
    app.include_router(review_router)
    app.include_router(categories_router)
    app.include_router(rules_router)
    app.include_router(llm_router)
    app.include_router(settings_router)
    app.include_router(data_router)
    app.include_router(accounts_router)
    app.include_router(stats_router)
    app.include_router(export_router)
    app.include_router(fx_router)
    app.include_router(logs_router)

    # -------------------------------------------------------------------------
    # Frontend Static Distribution & SPA Routing Fallback
    # -------------------------------------------------------------------------
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    from billapp.paths import get_frontend_dist_dir

    dist_dir = get_frontend_dist_dir()
    if dist_dir.is_dir() and (dist_dir / "index.html").is_file():
        logger.info("Mounting frontend static distribution from %s", dist_dir)
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/", include_in_schema=False)
        async def serve_index():
            return FileResponse(dist_dir / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa_fallback(request: Request, full_path: str):
            # Enforce absolute priority for API routes: never mask API 404s with HTML
            if full_path.startswith("api/") or full_path == "api":
                return JSONResponse(
                    status_code=404,
                    content={"code": "NOT_FOUND", "message": f"API endpoint /{full_path} not found"},
                )

            # Check if direct static file exists (e.g. favicon.ico, logo.png)
            target_path = (dist_dir / full_path).resolve()
            dist_resolved = dist_dir.resolve()
            try:
                target_path.relative_to(dist_resolved)
                if target_path.is_file():
                    return FileResponse(target_path)
            except ValueError:
                # Path traversal attempt outside dist_resolved
                return JSONResponse(status_code=403, content={"code": "FORBIDDEN", "message": "Access denied"})

            # Fallback to SPA entry point
            return FileResponse(dist_dir / "index.html")
    else:
        logger.debug("Frontend static dist directory not found at %s; skipping static mount.", dist_dir)

    return app
