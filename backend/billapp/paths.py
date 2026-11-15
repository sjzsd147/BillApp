"""Application paths resolution for database, user data, and static web distribution."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import uuid
from typing import Optional

APP_DIR_NAME = "BillApp"


def is_dir_writable(path: Path) -> bool:
    """Return True if directory exists or can be created and written to (thread-safe)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".billapp_probe_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def get_appdata_dir(platform: Optional[str] = None) -> Path:
    """Resolve writable cross-platform application data directory with safe fallbacks.

    Precedence:
    - Explicit ``BILLAPP_DATA_DIR`` override
    - Windows: ``%APPDATA%\\BillApp``, ``~\\AppData\\LocalLow\\BillApp``
    - macOS: ``~/Library/Application Support/BillApp``
    - Linux/POSIX: ``$XDG_DATA_HOME/BillApp``, ``~/.local/share/BillApp``
    - Fallbacks: ``~/.billapp``, frozen sibling ``billapp_data``, temp dir
    """
    plat = platform or sys.platform
    candidates: list[Path] = []

    custom_dir = (os.environ.get("BILLAPP_DATA_DIR") or "").strip()
    if custom_dir:
        candidates.append(Path(custom_dir).expanduser())

    try:
        home = Path.home()
    except Exception:
        home = None

    if plat == "win32":
        appdata = (os.environ.get("APPDATA") or "").strip()
        if appdata:
            candidates.append(Path(appdata) / APP_DIR_NAME)
        if home is not None:
            candidates.append(home / "AppData" / "LocalLow" / APP_DIR_NAME)
    elif plat == "darwin":
        if home is not None:
            candidates.append(home / "Library" / "Application Support" / APP_DIR_NAME)
    else:
        xdg_data_home = (os.environ.get("XDG_DATA_HOME") or "").strip()
        if xdg_data_home:
            candidates.append(Path(xdg_data_home).expanduser() / APP_DIR_NAME)
        elif home is not None:
            candidates.append(home / ".local" / "share" / APP_DIR_NAME)

    if home is not None:
        candidates.append(home / ".billapp")

    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "billapp_data")
    try:
        candidates.append(Path(tempfile.gettempdir()) / APP_DIR_NAME)
    except (OSError, FileNotFoundError):
        pass

    for candidate in candidates:
        if is_dir_writable(candidate):
            return candidate
    return candidates[0] if candidates else Path.cwd() / "billapp_data"


def get_default_db_path() -> Path:
    """Resolve standard database location (honoring BILLAPP_DB_PATH override)."""
    custom_db = (os.environ.get("BILLAPP_DB_PATH") or "").strip()
    if custom_db:
        return Path(custom_db).expanduser()
    return get_appdata_dir() / "bill.db"


def get_frontend_dist_dir() -> Path:
    """Resolve frontend production build distribution directory.

    Checks:
    1. Explicit ``BILLAPP_FRONTEND_DIR`` override (used by Electron sidecar / custom setups)
    2. PyInstaller frozen environment: ``sys._MEIPASS / 'web'`` or Electron ``../frontend-dist``
    3. Local backend package distribution: ``backend/billapp/web``
    4. Workspace repository root: ``<root>/frontend/dist``
    """
    env_dist = (os.environ.get("BILLAPP_FRONTEND_DIR") or "").strip()
    if env_dist:
        env_path = Path(env_dist).expanduser()
        if env_path.is_dir():
            return env_path

    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            bundled = Path(sys._MEIPASS) / "web"
            if bundled.is_dir():
                return bundled
        electron_resources_dist = Path(sys.executable).resolve().parent.parent / "frontend-dist"
        if electron_resources_dist.is_dir():
            return electron_resources_dist

    local_web = Path(__file__).resolve().parent / "web"
    if local_web.is_dir():
        return local_web

    workspace_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    return workspace_dist
