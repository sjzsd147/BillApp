"""Centralized logging configuration, secret/PII redaction, and diagnostic log export."""

from __future__ import annotations

from datetime import datetime
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import zipfile

from billapp.core.secrets import get_secret_storage_info
from billapp.llm.client import LLMConfig
from billapp.llm.prompts import collect_known_holder_names
from billapp.paths import get_appdata_dir

LOG_FILENAME = "launcher.log"
MAX_LOG_FILE_BYTES = 5 * 1024 * 1024  # 5 MB per rotated log file
LOG_BACKUP_COUNT = 3
MAX_READ_BYTES_PER_FILE = 4 * 1024 * 1024  # Read tail up to 4 MB per log file on export
MAX_TOTAL_EXPORT_BYTES = 16 * 1024 * 1024  # Hard cap on total uncompressed exported text
LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"

_active_log_path: Optional[Path] = None
_active_handler: Optional[RotatingFileHandler] = None


def format_local_log_timestamp(
    dt: Optional[datetime] = None,
    *,
    include_ms: bool = True,
) -> str:
    """Format a timezone-aware local timestamp (``YYYY-MM-DD HH:MM:SS[.mmm]±HH:MM``)."""
    local_dt = (dt if dt is not None else datetime.now()).astimezone()
    tz_raw = local_dt.strftime("%z")
    tz_fmt = f"{tz_raw[:3]}:{tz_raw[3:]}" if len(tz_raw) == 5 else tz_raw
    if include_ms:
        ms = local_dt.microsecond // 1000
        return f"{local_dt.strftime('%Y-%m-%d %H:%M:%S')}.{ms:03d}{tz_fmt}"
    return f"{local_dt.strftime('%Y-%m-%d %H:%M:%S')}{tz_fmt}"


class _LocalTimezoneFormatter(logging.Formatter):
    """Log formatter emitting local timestamps with millisecond precision and explicit UTC offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created).astimezone()
        if datefmt:
            return dt.strftime(datefmt)
        return format_local_log_timestamp(dt, include_ms=True)

# ---------------------------------------------------------------------------
# Always-on secret redaction patterns (NEVER disabled)
# ---------------------------------------------------------------------------

_RE_AUTH_BEARER = re.compile(r"(?i)(\bbearer\s+)([^\s\"',;]+)")
_RE_BILLAPP_TOKEN_HDR = re.compile(r"(?i)(x-billapp-token\s*[:=]\s*)([^\s\"',;]+)")
_RE_BOOT_TOKEN = re.compile(r"(?i)(boot_token=)([^\s&\"']+)")
_RE_SESSION_COOKIE = re.compile(r"(?i)(billapp_session=)([^\s;\"']+)")
_RE_ENV_SECRET = re.compile(r"(?i)(BILLAPP_(?:API_TOKEN|MASTER_KEY|BOOTSTRAP_TOKEN)\s*[:=]\s*)([^\s\"',;]+)")
_RE_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_RE_ENCRYPTED_SECRET = re.compile(r"\b(encv1|dpapi|b64):[A-Za-z0-9+/=_-]{12,}")
_RE_URL_CREDS = re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@")
_RE_KV_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret[_-]?key|password|passwd|pwd)\b\s*[\"']?\s*[:=]\s*[\"']?)([^\s\"',}\]]+)"
)

# ---------------------------------------------------------------------------
# PII redaction patterns (default ON)
# ---------------------------------------------------------------------------

_RE_CARD_SPACED = re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4,7}(?!\d)")
_RE_CARD_CONTIGUOUS = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_RE_CHINESE_ID = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_RE_CHINESE_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RE_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_gettempdir() -> Optional[Path]:
    try:
        return Path(tempfile.gettempdir())
    except (OSError, FileNotFoundError):
        return None


def get_log_candidates() -> List[Path]:
    """Return ordered candidate paths for the primary backend log file."""
    candidates: List[Path] = []
    try:
        candidates.append(get_appdata_dir() / LOG_FILENAME)
    except OSError:
        pass
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "billapp_data" / LOG_FILENAME)
    if sys.platform == "win32":
        try:
            candidates.append(Path.home() / "AppData" / "LocalLow" / "BillApp" / LOG_FILENAME)
        except Exception:
            pass
    temp_dir = _safe_gettempdir()
    if temp_dir is not None:
        candidates.append(temp_dir / "billapp_launcher.log")
    return candidates


def set_active_log_path(path: Optional[Path]) -> None:
    global _active_log_path
    _active_log_path = Path(path) if path is not None else None


def get_active_log_path() -> Optional[Path]:
    return _active_log_path


def get_default_log_path() -> Path:
    """Resolve the active or default backend log path."""
    if _active_log_path is not None:
        return _active_log_path
    candidates = get_log_candidates()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if candidates:
        return candidates[0]
    return Path.cwd() / LOG_FILENAME


def setup_file_logging(
    target_logger: Optional[logging.Logger] = None,
    extra_logger_names: Sequence[str] = ("billapp.app", "uvicorn.error"),
) -> Optional[Path]:
    """Configure rotating UTF-8 file logging under platform app data dir with safe fallbacks."""
    global _active_log_path, _active_handler
    primary = target_logger or logging.getLogger("billapp.launcher")
    if any(isinstance(h, logging.FileHandler) for h in primary.handlers):
        for h in primary.handlers:
            if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None):
                _active_log_path = Path(h.baseFilename)
                break
        return None

    for log_path in get_log_candidates():
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_path,
                maxBytes=MAX_LOG_FILE_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(_LocalTimezoneFormatter(LOG_FORMAT))
            primary.addHandler(handler)
            primary.setLevel(logging.INFO)
            for extra_name in extra_logger_names:
                extra_logger = logging.getLogger(extra_name)
                if not any(isinstance(h, logging.FileHandler) for h in extra_logger.handlers):
                    extra_logger.addHandler(handler)
                    if extra_logger.level == logging.NOTSET:
                        extra_logger.setLevel(logging.INFO)
            _active_log_path = log_path
            _active_handler = handler
            return log_path
        except OSError:
            continue
    return None


def attach_package_logging(handler: Optional[logging.Handler] = None) -> None:
    """Attach the active rotating log handler to the top-level ``billapp`` package logger.

    Ensures logs from ``billapp.llm.*``, ``billapp.core.*``, ``billapp.classify.*``,
    ``billapp.api.*``, etc. are persisted to ``launcher.log`` without duplicating entries
    from ``billapp.launcher`` or ``billapp.app``.
    """
    active = handler or _active_handler
    if active is None:
        return

    pkg_logger = logging.getLogger("billapp")
    if active not in pkg_logger.handlers and not any(
        isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == getattr(active, "baseFilename", None)
        for h in pkg_logger.handlers
    ):
        pkg_logger.addHandler(active)
    if pkg_logger.level == logging.NOTSET or pkg_logger.level > logging.INFO:
        pkg_logger.setLevel(logging.INFO)

    # Prevent duplicate emissions for loggers that already hold the same file handler directly.
    for direct_name in ("billapp.launcher", "billapp.app"):
        child = logging.getLogger(direct_name)
        if any(isinstance(h, logging.FileHandler) for h in child.handlers):
            child.propagate = False


def flush_active_logs() -> None:
    """Flush all FileHandlers attached to BillApp loggers before reading log files."""
    seen: set[int] = set()
    for name in ("billapp", "billapp.launcher", "billapp.app", "uvicorn.error"):
        lgr = logging.getLogger(name)
        for h in list(lgr.handlers):
            if id(h) in seen:
                continue
            seen.add(id(h))
            try:
                h.flush()
            except Exception:
                pass


def discover_backend_log_files(primary_log_path: Optional[Path] = None) -> List[Path]:
    """Return existing backend log file and its rotated backups in chronological-friendly order.

    Only returns regular, non-symlink files matching ``<stem>`` or ``<stem>.<digit>`` inside
    the active log directory.
    """
    target = Path(primary_log_path) if primary_log_path is not None else get_default_log_path()
    log_dir = target.parent
    base_name = target.name
    if not log_dir.is_dir():
        return []

    found: List[Tuple[int, Path]] = []
    # Current active log (index 0) and rotated backups (1..9)
    candidates = [log_dir / base_name] + [
        log_dir / f"{base_name}.{i}" for i in range(1, max(LOG_BACKUP_COUNT + 2, 10))
    ]
    for idx, p in enumerate(candidates):
        try:
            if p.is_symlink() or not p.is_file():
                continue
            # Ensure resolved path remains inside log_dir
            p.resolve().relative_to(log_dir.resolve())
            found.append((idx, p))
        except (OSError, ValueError):
            continue

    # Primary file first (idx 0), followed by .1, .2, .3
    found.sort(key=lambda item: item[0])
    return [p for _, p in found]


def sanitize_archive_filename(raw_name: str, fallback: str = "log.txt") -> str:
    """Sanitize a filename for safe placement inside a ZIP archive (no path traversal)."""
    base = Path(str(raw_name or "").replace("\\", "/")).name.strip()
    cleaned = _RE_SAFE_FILENAME.sub("_", base).strip(".")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned[:100]


def redact_log_text(
    text: str,
    *,
    holder_names: Optional[Iterable[str]] = None,
    redact_pii: bool = True,
) -> str:
    """Redact credentials/tokens (always) and optional personal/path PII from log text."""
    if not text:
        return ""

    out = text
    # 1. Mandatory credential & token redaction
    out = _RE_AUTH_BEARER.sub(r"\1[REDACTED]", out)
    out = _RE_BILLAPP_TOKEN_HDR.sub(r"\1[REDACTED]", out)
    out = _RE_BOOT_TOKEN.sub(r"\1[REDACTED]", out)
    out = _RE_SESSION_COOKIE.sub(r"\1[REDACTED]", out)
    out = _RE_ENV_SECRET.sub(r"\1[REDACTED]", out)
    out = _RE_OPENAI_KEY.sub("sk-[REDACTED]", out)
    out = _RE_ENCRYPTED_SECRET.sub(r"\1:[REDACTED]", out)
    out = _RE_URL_CREDS.sub(r"\1[REDACTED]@", out)
    out = _RE_KV_SECRET.sub(r"\1[REDACTED]", out)

    if not redact_pii:
        return out

    # 2. Financial & identity PII redaction
    out = _RE_CHINESE_ID.sub("[ID_REDACTED]", out)
    out = _RE_CARD_SPACED.sub("[CARD_REDACTED]", out)
    out = _RE_CARD_CONTIGUOUS.sub("[CARD_REDACTED]", out)
    out = _RE_CHINESE_MOBILE.sub("[PHONE_REDACTED]", out)

    # 3. Known account holder names from local DB
    if holder_names:
        names = sorted(
            {str(n).strip() for n in holder_names if n and len(str(n).strip()) >= 2},
            key=len,
            reverse=True,
        )
        for name in names:
            out = out.replace(name, "[USER]")

    # 4. Home directory / OS username masking in paths
    try:
        home_str = str(Path.home())
        if home_str and len(home_str) > 3:
            home_patterns = {home_str, home_str.replace("\\", "/")}
            for hp in sorted(home_patterns, key=len, reverse=True):
                if sys.platform == "win32":
                    out = re.sub(re.escape(hp), "~", out, flags=re.IGNORECASE)
                else:
                    out = out.replace(hp, "~")
    except Exception:
        pass

    return out


def _read_file_tail_utf8(path: Path, max_bytes: int = MAX_READ_BYTES_PER_FILE) -> Tuple[str, bool]:
    """Read up to ``max_bytes`` from the end of a file as UTF-8, returning (text, truncated)."""
    size = path.stat().st_size
    truncated = False
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            raw = fh.read(max_bytes)
            # Drop partial first UTF-8 line if we sought into the middle
            nl = raw.find(b"\n")
            if nl != -1:
                raw = raw[nl + 1 :]
            truncated = True
        else:
            raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text = f"[... truncated earlier {size - len(raw)} bytes ...]\n{text}"
    return text, truncated


def get_log_file_metadata(files: Sequence[Path]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for p in files:
        try:
            st = p.stat()
            items.append(
                {
                    "name": p.name,
                    "size_bytes": int(st.st_size),
                    "modified_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        except OSError:
            continue
    return items


def read_backend_log_tail(
    files: Sequence[Path],
    *,
    max_lines: int = 120,
    holder_names: Optional[Iterable[str]] = None,
    redact_pii: bool = True,
) -> List[str]:
    """Return the latest ``max_lines`` sanitized lines from the primary backend log."""
    if not files or max_lines <= 0:
        return []
    primary = files[0]
    try:
        text, _ = _read_file_tail_utf8(primary, max_bytes=256 * 1024)
    except OSError:
        return []
    lines = text.splitlines()
    tail = lines[-max_lines:]
    return [redact_log_text(line, holder_names=holder_names, redact_pii=redact_pii) for line in tail]


def _safe_table_count(db: sqlite3.Connection, table: str) -> int:
    try:
        row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def build_diagnostics_summary(
    db: sqlite3.Connection,
    *,
    app_version: str,
    db_path: str,
    log_files: Sequence[Path],
    desktop_meta: Optional[Dict[str, Any]] = None,
    frontend_log_count: int = 0,
    desktop_log_files: Sequence[Dict[str, Any]] = (),
    redact_pii: bool = True,
) -> Dict[str, Any]:
    """Collect safe, non-sensitive environment & aggregate diagnostic metrics."""
    db_size_bytes = 0
    if db_path and db_path != ":memory:" and os.path.exists(db_path):
        try:
            db_size_bytes = os.path.getsize(db_path)
        except OSError:
            db_size_bytes = 0

    llm_conf = LLMConfig.load_from_db(db)
    storage_info = get_secret_storage_info()

    masked_db_path = redact_log_text(db_path, redact_pii=redact_pii) if db_path else ""
    masked_log_dir = (
        redact_log_text(str(log_files[0].parent), redact_pii=redact_pii)
        if log_files
        else redact_log_text(str(get_default_log_path().parent), redact_pii=redact_pii)
    )

    safe_desktop_meta: Optional[Dict[str, Any]] = None
    if isinstance(desktop_meta, dict) and desktop_meta:
        safe_desktop_meta = {}
        for k in ("platform", "arch", "appVersion", "isPackaged", "installMode", "versions"):
            if k in desktop_meta:
                safe_desktop_meta[k] = desktop_meta[k]

    local_now = datetime.now().astimezone()
    tz_raw = local_now.strftime("%z")
    tz_label = f"UTC{tz_raw[:3]}:{tz_raw[3:]}" if len(tz_raw) == 5 else (local_now.tzname() or "LOCAL")

    return {
        "exported_at": format_local_log_timestamp(local_now, include_ms=False),
        "app_version": app_version,
        "redaction": {
            "secrets_redacted": True,
            "pii_redacted": bool(redact_pii),
        },
        "runtime": {
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "timezone": tz_label,
        },
        "desktop": safe_desktop_meta,
        "database": {
            "path": masked_db_path,
            "size_bytes": db_size_bytes,
            "counts": {
                "transactions": _safe_table_count(db, "transactions"),
                "accounts": _safe_table_count(db, "accounts"),
                "import_batches": _safe_table_count(db, "import_batches"),
                "categories": _safe_table_count(db, "categories"),
                "rules": _safe_table_count(db, "rules"),
                "match_links": _safe_table_count(db, "match_links"),
                "bank_profiles": _safe_table_count(db, "bank_profiles"),
                "fx_rates": _safe_table_count(db, "fx_rates"),
            },
        },
        "llm": {
            "enabled": bool(llm_conf.enabled),
            "configured": bool(llm_conf.base_url and llm_conf.api_key),
            "model": llm_conf.model,
            "timeout_sec": llm_conf.timeout_sec,
            "has_api_key": bool(llm_conf.api_key and llm_conf.api_key.strip()),
            "api_key_decryption_failed": bool(llm_conf.api_key_decryption_failed),
            "secret_storage_backend": storage_info.get("backend", ""),
        },
        "logs": {
            "backend_dir": masked_log_dir,
            "backend_files": get_log_file_metadata(log_files),
            "desktop_files": list(desktop_log_files),
            "frontend_entries": int(frontend_log_count),
        },
    }


def format_diagnostics_text(summary: Dict[str, Any]) -> str:
    """Format diagnostic summary dictionary as a clean human-readable text report."""
    rt = summary.get("runtime", {})
    tz_note = f" (本地时区 {rt.get('timezone')})" if rt.get("timezone") else ""
    lines = [
        "============================================================",
        "BillApp (账单助手) 诊断日志摘要",
        "============================================================",
        f"导出时间: {summary.get('exported_at', '')}{tz_note}",
        f"应用版本: v{summary.get('app_version', '')}",
        f"隐私脱敏: 密钥/令牌=已强制脱敏, 个人敏感信息/路径={'已脱敏' if summary.get('redaction', {}).get('pii_redacted') else '未启用'}",
        "",
        "[运行环境]",
    ]
    lines.append(f"  操作系统: {rt.get('os', '')} {rt.get('os_release', '')} ({rt.get('machine', '')})")
    lines.append(f"  Python:   {rt.get('python_version', '')} (frozen={rt.get('frozen', False)})")

    desktop = summary.get("desktop")
    if isinstance(desktop, dict):
        versions = desktop.get("versions") if isinstance(desktop.get("versions"), dict) else {}
        lines.append(
            f"  桌面外壳: Electron {versions.get('electron', '-')} / Chrome {versions.get('chrome', '-')} / Node {versions.get('node', '-')}"
        )
        lines.append(
            f"  安装形态: {desktop.get('installMode', '-')} (packaged={desktop.get('isPackaged', False)}, appVersion={desktop.get('appVersion', '-')})"
        )

    db_info = summary.get("database", {})
    counts = db_info.get("counts", {})
    lines.extend(
        [
            "",
            "[本地数据库概要（仅统计计数，不含交易内容）]",
            f"  路径: {db_info.get('path', '-')}",
            f"  大小: {db_info.get('size_bytes', 0)} bytes",
            (
                f"  计数: transactions={counts.get('transactions', 0)}, "
                f"accounts={counts.get('accounts', 0)}, "
                f"batches={counts.get('import_batches', 0)}, "
                f"rules={counts.get('rules', 0)}, "
                f"match_links={counts.get('match_links', 0)}, "
                f"profiles={counts.get('bank_profiles', 0)}"
            ),
        ]
    )

    llm = summary.get("llm", {})
    lines.extend(
        [
            "",
            "[AI 配置状态]",
            f"  已启用: {llm.get('enabled', False)} | 已配置: {llm.get('configured', False)} | 模型: {llm.get('model', '-') or '-'}",
            f"  加密后端: {llm.get('secret_storage_backend', '-') or '-'} | 解密异常: {llm.get('api_key_decryption_failed', False)}",
        ]
    )

    logs_info = summary.get("logs", {})
    lines.extend(["", "[包含的日志文件]"])
    backend_files = logs_info.get("backend_files", [])
    if backend_files:
        for item in backend_files:
            lines.append(
                f"  - backend/{item['name']} ({item['size_bytes']} bytes, updated {item['modified_at']})"
            )
    else:
        lines.append("  - backend/ (当前无持久化后端日志文件)")
    for item in logs_info.get("desktop_files", []):
        lines.append(
            f"  - desktop/{item.get('name', 'desktop.log')} ({item.get('size_bytes', 0)} bytes)"
        )
    if logs_info.get("frontend_entries", 0) > 0:
        lines.append(f"  - frontend/runtime.log ({logs_info['frontend_entries']} entries)")
    lines.append("============================================================")
    return "\n".join(lines) + "\n"


def format_frontend_logs(
    entries: Sequence[Dict[str, Any]],
    *,
    holder_names: Optional[Iterable[str]] = None,
    redact_pii: bool = True,
) -> str:
    """Format captured frontend runtime error/warning entries into plain log lines."""
    if not entries:
        return ""
    lines: List[str] = []
    for entry in entries:
        ts = str(entry.get("timestamp") or "").strip() or "-"
        lvl = str(entry.get("level") or "error").upper()
        src = str(entry.get("source") or "frontend").strip()
        msg = str(entry.get("message") or "").strip()
        stack = str(entry.get("stack") or "").strip()
        line = f"{ts} [{lvl}] [{src}] {msg}"
        if stack:
            line = f"{line}\n{stack}"
        lines.append(redact_log_text(line, holder_names=holder_names, redact_pii=redact_pii))
    return "\n".join(lines) + "\n"


def build_log_export_bundle(
    db: sqlite3.Connection,
    *,
    app_version: str,
    db_path: str,
    primary_log_path: Optional[Path] = None,
    export_format: str = "zip",
    include_diagnostics: bool = True,
    redact_pii: bool = True,
    frontend_logs: Sequence[Dict[str, Any]] = (),
    desktop_logs: Sequence[Dict[str, Any]] = (),
    desktop_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[bytes, str, str]:
    """Assemble sanitized diagnostic logs as either a ZIP archive or a merged UTF-8 ``.log`` file.

    Returns ``(content_bytes, media_type, filename)``.
    """
    flush_active_logs()
    holder_names = collect_known_holder_names(db) if redact_pii else []
    backend_files = discover_backend_log_files(primary_log_path)

    # Read & redact backend logs within total size budget
    remaining_budget = MAX_TOTAL_EXPORT_BYTES
    backend_sections: List[Tuple[str, str]] = []
    for log_file in backend_files:
        if remaining_budget <= 0:
            break
        try:
            read_limit = min(MAX_READ_BYTES_PER_FILE, remaining_budget)
            raw_text, _ = _read_file_tail_utf8(log_file, max_bytes=read_limit)
            sanitized = redact_log_text(raw_text, holder_names=holder_names, redact_pii=redact_pii)
            backend_sections.append((sanitize_archive_filename(log_file.name, LOG_FILENAME), sanitized))
            remaining_budget -= len(sanitized.encode("utf-8", errors="ignore"))
        except OSError:
            continue

    # Redact desktop logs supplied by Electron main process
    desktop_sections: List[Tuple[str, str]] = []
    desktop_meta_files: List[Dict[str, Any]] = []
    used_desktop_names: set[str] = set()
    for idx, item in enumerate(desktop_logs):
        if remaining_budget <= 0:
            break
        raw_content = str(item.get("content") or "")
        if not raw_content:
            continue
        safe_name = sanitize_archive_filename(str(item.get("name") or f"desktop-{idx + 1}.log"), f"desktop-{idx + 1}.log")
        if safe_name in used_desktop_names:
            safe_name = f"{idx + 1}_{safe_name}"
        used_desktop_names.add(safe_name)
        encoded = raw_content.encode("utf-8", errors="replace")
        if len(encoded) > remaining_budget:
            encoded = encoded[-remaining_budget:]
            raw_content = "[... truncated due to export size limit ...]\n" + encoded.decode("utf-8", errors="replace")
        sanitized = redact_log_text(raw_content, holder_names=holder_names, redact_pii=redact_pii)
        desktop_sections.append((safe_name, sanitized))
        desktop_meta_files.append(
            {
                "name": safe_name,
                "size_bytes": len(sanitized.encode("utf-8")),
            }
        )
        remaining_budget -= len(sanitized.encode("utf-8", errors="ignore"))

    frontend_text = format_frontend_logs(frontend_logs, holder_names=holder_names, redact_pii=redact_pii)

    diagnostics_summary: Optional[Dict[str, Any]] = None
    diagnostics_text = ""
    if include_diagnostics:
        diagnostics_summary = build_diagnostics_summary(
            db,
            app_version=app_version,
            db_path=db_path,
            log_files=backend_files,
            desktop_meta=desktop_meta,
            frontend_log_count=len(frontend_logs),
            desktop_log_files=desktop_meta_files,
            redact_pii=redact_pii,
        )
        diagnostics_text = format_diagnostics_text(diagnostics_summary)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if export_format == "log":
        parts: List[str] = []
        if include_diagnostics and diagnostics_text:
            parts.append(diagnostics_text)
        if backend_sections:
            for name, content in backend_sections:
                parts.append(f"\n--- [BACKEND LOG: {name}] ---\n")
                parts.append(content if content.endswith("\n") else f"{content}\n")
        else:
            parts.append("\n--- [BACKEND LOG] ---\n(当前暂无持久化后端日志记录)\n")

        if desktop_sections:
            for name, content in desktop_sections:
                parts.append(f"\n--- [DESKTOP LOG: {name}] ---\n")
                parts.append(content if content.endswith("\n") else f"{content}\n")

        if frontend_text:
            parts.append("\n--- [FRONTEND RUNTIME LOG] ---\n")
            parts.append(frontend_text)

        merged_bytes = "".join(parts).encode("utf-8")
        return merged_bytes, "text/plain; charset=utf-8", f"billapp-logs-{timestamp}.log"

    # Default: structured ZIP bundle
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        if include_diagnostics and diagnostics_summary is not None:
            zf.writestr("diagnostics.txt", diagnostics_text.encode("utf-8"))
            zf.writestr(
                "diagnostics.json",
                json.dumps(diagnostics_summary, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        if backend_sections:
            for name, content in backend_sections:
                zf.writestr(f"backend/{name}", content.encode("utf-8"))
        else:
            zf.writestr(
                "backend/launcher.log",
                "(当前暂无持久化后端日志记录)\n".encode("utf-8"),
            )
        for name, content in desktop_sections:
            zf.writestr(f"desktop/{name}", content.encode("utf-8"))
        if frontend_text:
            zf.writestr("frontend/runtime.log", frontend_text.encode("utf-8"))

    return buf.getvalue(), "application/zip", f"billapp-logs-{timestamp}.zip"
