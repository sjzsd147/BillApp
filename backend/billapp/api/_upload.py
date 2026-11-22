"""Bounded chunked file upload reader helpers to prevent memory exhaustion DoS."""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

MAX_STATEMENT_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB for statement files (CSV/XLSX/PDF/ZIP)
MAX_BACKUP_UPLOAD_BYTES = 200 * 1024 * 1024     # 200 MB for SQLite backup restore files
_UPLOAD_CHUNK_BYTES = 1024 * 1024               # 1 MB streaming chunks


async def read_upload_limited(
    file: UploadFile,
    max_bytes: int = MAX_STATEMENT_UPLOAD_BYTES,
) -> bytes:
    """Read an uploaded file in bounded chunks, rejecting payloads over ``max_bytes`` with HTTP 413."""
    chunks: list[bytes] = []
    total = 0

    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            max_mb = max_bytes // (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail={
                    "detail": f"上传文件超过大小限制（最大允许 {max_mb} MB）",
                    "error_code": "FILE_TOO_LARGE",
                },
            )
        chunks.append(chunk)

    return b"".join(chunks)
