"""FastAPI Application Factory, Lifespan Lifecycle, Middleware & Error Handlers."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
import sqlite3
from typing import Any, Generator, Optional, Union

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from billapp.classify.categories import CategoryService
from billapp.core.db import create_connection, get_default_db_path, init_db

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
    init_conn = init_db(db_path)
    try:
        CategoryService.init_builtin_categories(init_conn)
        _cleanup_legacy_builtin_profiles(init_conn)
        init_conn.commit()
    finally:
        init_conn.close()

    yield

    # Clean shutdown checkpoint
    try:
        cleanup_conn = create_connection(db_path)
        cleanup_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        cleanup_conn.close()
    except Exception:
        pass


# =========================================================================
# Application Factory
# =========================================================================

def create_app(db_path: Optional[Union[str, Path]] = None) -> FastAPI:
    """FastAPI application factory supporting configurable SQLite database path."""
    app = FastAPI(
        title="账单综合管理系统 API",
        version="1.0.0",
        description="多来源账单综合管理程序后端 REST API",
        lifespan=lifespan,
    )
    app.state.db_path = db_path if db_path is not None else get_default_db_path()

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "*",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Custom Exception Handlers
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": "字段校验错误", "details": exc.errors()},
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_busy_handler(request: Request, exc: sqlite3.OperationalError):
        err_msg = str(exc).lower()
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
        return {"status": "ok", "version": "1.0.0"}

    @app.get("/api/meta", tags=["system"])
    async def get_system_meta(db: sqlite3.Connection = Depends(get_db_conn)):
        pending_review_count = db.execute(
            "SELECT COUNT(*) FROM match_links WHERE status = 'pending'"
        ).fetchone()[0]
        categories_tree = CategoryService.get_tree(db)
        accounts_rows = db.execute("SELECT id, kind, bank, tail, alias FROM accounts").fetchall()
        accounts_list = [dict(r) for r in accounts_rows]

        return {
            "version": "1.0.0",
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
