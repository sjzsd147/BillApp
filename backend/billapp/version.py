"""Single-source-of-truth application version loader (reads root version.json)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import List


def _version_file_candidates() -> List[Path]:
    candidates: List[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "version.json")
        candidates.append(Path(sys.executable).resolve().parent / "version.json")

    here = Path(__file__).resolve()
    # Repo root: <repo>/version.json (from backend/billapp/version.py)
    candidates.append(here.parent.parent.parent / "version.json")
    # Package fallback
    candidates.append(here.parent / "version.json")
    return candidates


def get_app_version(default: str = "0.0.0") -> str:
    """Load the application version from the repository/bundle ``version.json`` config."""
    for candidate in _version_file_candidates():
        try:
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                ver = str(data.get("version") or "").strip().lstrip("vV")
                if ver:
                    return ver
        except Exception:
            continue
    return default


APP_VERSION: str = get_app_version()
