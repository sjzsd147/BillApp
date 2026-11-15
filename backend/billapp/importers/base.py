"""Abstract Importer interface, FileProbe abstraction, and parser results."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Union

from billapp.core.models import (
    AccountCreate,
    ParseResult,
    SelfCheckResult,
    TransactionCreate,
)


class FileProbe:
    """Encapsulates uploaded file content, metadata, SHA1, and format inspection."""

    def __init__(
        self,
        file_path_or_bytes: Union[str, Path, bytes],
        file_name: Optional[str] = None,
        encoding: Optional[str] = None,
        password: Optional[str] = None,
    ):
        if isinstance(file_path_or_bytes, bytes):
            self.file_path: Optional[Path] = None
            self._bytes: bytes = file_path_or_bytes
            self.file_name: str = file_name or "uploaded_file"
            self.file_size: int = len(self._bytes)
        else:
            path = Path(file_path_or_bytes)
            self.file_path = path
            self.file_name = file_name or path.name
            self.file_size = path.stat().st_size
            self._bytes = path.read_bytes()

        self.file_sha1: str = hashlib.sha1(self._bytes).hexdigest()
        self.encoding: Optional[str] = encoding
        self.password: Optional[str] = password

    @property
    def content(self) -> bytes:
        return self._bytes

    def is_csv(self) -> bool:
        lower = self.file_name.lower()
        return lower.endswith(".csv") or lower.endswith(".txt")

    def is_excel(self) -> bool:
        lower = self.file_name.lower()
        return lower.endswith(".xlsx") or lower.endswith(".xls")

    def is_pdf(self) -> bool:
        return self.file_name.lower().endswith(".pdf") or self._bytes.startswith(b"%PDF")

    def is_zip(self) -> bool:
        lower = self.file_name.lower()
        if lower.endswith(".zip"):
            return True
        if self.is_excel():
            return False
        return (
            self._bytes.startswith(b"PK\x03\x04") or self._bytes.startswith(b"PK\x05\x06")
        ) and b"[Content_Types].xml" not in self._bytes[:4096]

    def read_text(
        self,
        encoding: Optional[str] = None,
        max_lines: Optional[int] = None,
        errors: str = "replace",
    ) -> list[str]:
        """Decode lines from the file content using the specified or probed encoding."""
        enc = encoding or self.encoding or "utf-8"
        try:
            text = self._bytes.decode(enc, errors=errors)
        except Exception:
            text = self._bytes.decode("utf-8", errors="replace")
        # Normalize CR/CRLF to LF so csv.reader never chokes on unquoted bare '\r'
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines(keepends=True)
        if max_lines is not None:
            return lines[:max_lines]
        return lines


class Importer(ABC):
    """Abstract Base Class for statement importers."""

    source_id: str
    source_name: str

    @abstractmethod
    def detect(self, probe: FileProbe) -> float:
        """Evaluate how likely this importer handles the file (confidence 0.0 - 1.0)."""
        pass

    @abstractmethod
    def parse(self, probe: FileProbe) -> ParseResult:
        """Parse the statement into standardized transactions, accounts, and self-checks."""
        pass
