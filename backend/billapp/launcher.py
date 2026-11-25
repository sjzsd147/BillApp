"""Cross-platform FastAPI Backend Sidecar & Browser Launcher for BillApp."""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure backend package directory is in sys.path when invoked directly
backend_root = str(Path(__file__).resolve().parent.parent)
if backend_root not in sys.path:
    sys.path.insert(0, backend_root)

import argparse
import ctypes
import json
import logging
import os
import secrets
import signal
import socket
import tempfile
import threading
import time
import urllib.request
import webbrowser
from typing import Any, Optional

import uvicorn
from billapp.app import create_app
from billapp.core.logs import attach_package_logging, setup_file_logging
from billapp.paths import get_appdata_dir, get_default_db_path, get_frontend_dist_dir

logger = logging.getLogger("billapp.launcher")
MUTEX_NAME = r"Global\BillApp_SingleInstance_Mutex"
MUTEX_FALLBACK_NAME = r"Local\BillApp_SingleInstance_Mutex"
WINDOW_TITLE = "账单助手"
READY_PREFIX = "BILLAPP_BACKEND_READY "

_MUTEX_HANDLE: Optional[int] = None


def _safe_gettempdir() -> Optional[Path]:
    """Return system temp directory path without crashing if all temp candidates are unwritable."""
    try:
        return Path(tempfile.gettempdir())
    except (OSError, FileNotFoundError):
        return None


def _ensure_usable_tempdir() -> Optional[Path]:
    """Guarantee tempfile.gettempdir() resolves to a writable directory."""
    temp_dir = _safe_gettempdir()
    if temp_dir is not None:
        return temp_dir
    fallback = get_appdata_dir() / "tmp"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(fallback)
        os.environ["TEMP"] = str(fallback)
        os.environ["TMP"] = str(fallback)
        return fallback
    except OSError:
        return None


def _setup_file_logging() -> Optional[Path]:
    """Configure rotating file logging under platform app data dir (with safe fallbacks)."""
    return setup_file_logging(target_logger=logger)


def acquire_single_instance_mutex() -> bool:
    """Acquire named Windows mutex (no-op returning True on macOS/Linux; Electron manages app lock)."""
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True

    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    # Attempt Global mutex first; fallback to Local
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    err = kernel32.GetLastError()
    if not handle or err == 5:  # ERROR_ACCESS_DENIED
        handle = kernel32.CreateMutexW(None, True, MUTEX_FALLBACK_NAME)
        err = kernel32.GetLastError()

    if err == 183:  # ERROR_ALREADY_EXISTS
        logger.info("Another instance of BillApp is already running.")
        if handle:
            kernel32.CloseHandle(handle)
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        return False

    _MUTEX_HANDLE = handle
    return True


def release_single_instance_mutex() -> None:
    """Release single instance mutex upon application termination."""
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE and sys.platform == "win32":
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(_MUTEX_HANDLE)
        _MUTEX_HANDLE = None


def find_free_port(start_port: int = 8000, max_attempts: int = 50) -> int:
    """Find an available TCP port on 127.0.0.1 (or OS ephemeral port when start_port <= 0)."""
    if start_port <= 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _ensure_stdio() -> None:
    """Ensure sys.stdout and sys.stderr are writable streams."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _format_url_host(host: str) -> str:
    h = (host or "127.0.0.1").strip()
    if h in ("", "0.0.0.0"):
        return "127.0.0.1"
    if h == "::":
        return "[::1]"
    if ":" in h and not (h.startswith("[") and h.endswith("]")):
        return f"[{h}]"
    return h


def wait_for_server(
    port: int,
    timeout: float = 10.0,
    server_thread: Optional[threading.Thread] = None,
    host: str = "127.0.0.1",
) -> bool:
    """Poll health check endpoint (bypassing HTTP proxies) until server is ready or timeout expires."""
    url = f"http://{_format_url_host(host)}:{port}/api/health"
    start_time = time.time()
    while time.time() - start_time < timeout:
        if server_thread is not None and not server_thread.is_alive():
            return False
        try:
            with _NO_PROXY_OPENER.open(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def _start_stdin_watchdog(server: Any) -> threading.Thread:
    """Watch parent process stdin pipe and trigger clean shutdown when EOF arrives."""

    def _watch() -> None:
        try:
            while not server.should_exit:
                chunk = sys.stdin.readline() if sys.stdin else ""
                if chunk == "":
                    logger.info("Parent stdin closed (EOF); requesting graceful backend shutdown.")
                    server.should_exit = True
                    break
        except Exception as exc:
            logger.info("Stdin watchdog ended (%s); requesting backend shutdown.", exc)
            server.should_exit = True

    t = threading.Thread(target=_watch, name="billapp-stdin-watchdog", daemon=True)
    t.start()
    return t


def _register_signal_handlers(server: Any) -> None:
    """Register SIGTERM/SIGINT handlers so sidecar shuts down uvicorn cleanly."""

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; shutting down backend server.", signum)
        server.should_exit = True

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass


def main() -> None:
    _ensure_stdio()
    log_path = _setup_file_logging()
    attach_package_logging()
    _ensure_usable_tempdir()
    logger.info("=== BillApp backend starting (frozen=%s, argv=%s) ===", getattr(sys, "frozen", False), sys.argv)

    parser = argparse.ArgumentParser(description="BillApp Backend Sidecar & Launcher")
    parser.add_argument("--port", type=int, default=0, help="Server port (0 for random OS ephemeral port, default: 0)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--db", type=str, default=None, help="Custom SQLite database path")
    parser.add_argument("--frontend-dir", type=str, default=None, help="Override static frontend distribution path")
    parser.add_argument("--sidecar", action="store_true", help="Run as Electron-managed headless backend sidecar")
    parser.add_argument("--watch-stdin", action="store_true", help="Exit cleanly when stdin closes (parent process exited)")
    parser.add_argument("--no-browser", action="store_true", help="Run HTTP backend without launching system browser")
    parser.add_argument("--browser", action="store_true", help="Open application in default web browser")
    parser.add_argument("--selftest", action="store_true", help="Start server, verify /api/health, and exit")
    parser.add_argument("--dev", action="store_true", help="Open Vite dev URL (http://127.0.0.1:5173) in browser mode")
    parser.add_argument("--frontend-url", type=str, default=None, help="Custom frontend URL to open in browser mode")
    args = parser.parse_args()

    if args.frontend_dir:
        os.environ["BILLAPP_FRONTEND_DIR"] = args.frontend_dir

    standalone_browser = not args.sidecar and not args.no_browser and not args.selftest
    held_mutex = False
    if standalone_browser:
        if not acquire_single_instance_mutex():
            logger.warning("Single-instance mutex already held; exiting.")
            print("账单助手 已在运行中。")
            sys.exit(0)
        held_mutex = True

    server: Optional[uvicorn.Server] = None
    server_thread: Optional[threading.Thread] = None
    try:
        if args.port <= 0:
            port = find_free_port(0)
        else:
            port = args.port

        # Ensure per-launch local session authentication token (never passed via CLI argv)
        api_token = (os.environ.get("BILLAPP_API_TOKEN") or "").strip() or None
        boot_token: Optional[str] = None
        if standalone_browser and not args.dev and not api_token:
            api_token = secrets.token_hex(32)
            boot_token = secrets.token_urlsafe(24)
        elif args.dev:
            os.environ["BILLAPP_DEV_MODE"] = "1"

        resolved_db = args.db or str(get_default_db_path())
        dist_dir = get_frontend_dist_dir()
        logger.info(
            "Creating app: host=%s, port=%s, db=%s, dist_dir=%s (exists=%s), token_enabled=%s",
            args.host,
            port,
            resolved_db,
            dist_dir,
            dist_dir.is_dir(),
            bool(api_token),
        )
        app = create_app(
            db_path=resolved_db,
            api_token=api_token,
            bootstrap_token=boot_token,
            log_path=log_path,
        )

        config = uvicorn.Config(
            app=app,
            host=args.host,
            port=port,
            log_level="warning",
            log_config=None,
            use_colors=False,
        )
        server = uvicorn.Server(config)
        _register_signal_handlers(server)

        def _run_server() -> None:
            try:
                server.run()
            except BaseException as exc:
                if not isinstance(exc, SystemExit):
                    logger.exception("Backend server exited unexpectedly: %s", exc)

        server_thread = threading.Thread(target=_run_server, name="billapp-uvicorn", daemon=True)
        server_thread.start()

        if not wait_for_server(port, timeout=15.0, server_thread=server_thread, host=args.host):
            msg = f"Failed to start BillApp backend server (see {log_path or 'launcher.log'})."
            logger.error("%s port=%s, db=%s", msg, port, resolved_db)
            print(msg, file=sys.stderr, flush=True)
            sys.exit(1)

        base_url = f"http://{_format_url_host(args.host)}:{port}"
        logger.info("Backend server ready at %s", base_url)

        if args.selftest:
            logger.info("Self-test passed on port %s.", port)
            print("Self-test passed: server health is OK.", flush=True)
            server.should_exit = True
            server_thread.join(timeout=3.0)
            sys.exit(0)

        if args.sidecar:
            ready_payload = {
                "port": port,
                "host": args.host,
                "url": base_url,
                "db_path": str(Path(resolved_db).resolve()),
                "frontend_dist": str(dist_dir),
            }
            print(f"{READY_PREFIX}{json.dumps(ready_payload, ensure_ascii=False)}", flush=True)

        if args.watch_stdin:
            _start_stdin_watchdog(server)

        if standalone_browser:
            if args.dev:
                app_url = "http://127.0.0.1:5173"
                logger.info("Opening default browser at %s", app_url)
            elif args.frontend_url:
                app_url = args.frontend_url
                logger.info("Opening default browser at %s", app_url)
            elif boot_token:
                app_url = f"{base_url}/?boot_token={boot_token}"
                logger.info("Opening default browser at %s (with one-time session bootstrap)", base_url)
            else:
                app_url = base_url
                logger.info("Opening default browser at %s", app_url)
            webbrowser.open(app_url)

        try:
            while not server.should_exit and server_thread.is_alive():
                time.sleep(0.25)
        except KeyboardInterrupt:
            server.should_exit = True
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unhandled fatal error in BillApp backend launcher")
        raise
    finally:
        if server is not None:
            server.should_exit = True
        if server_thread is not None and server_thread.is_alive():
            server_thread.join(timeout=3.0)
        if held_mutex:
            release_single_instance_mutex()


if __name__ == "__main__":
    main()
