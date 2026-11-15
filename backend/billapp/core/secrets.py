"""Secret storage management using Windows DPAPI with cross-platform fallback.

Stores encrypted secrets prefixed with:
- "dpapi:<base64>" on Windows (encrypted with current user credentials)
- "b64:<base64>" on non-Windows or test fallbacks
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

DPAPI_PREFIX = "dpapi:"
B64_PREFIX = "b64:"


class SecretDecryptionError(ValueError):
    """Raised when an encrypted secret cannot be decrypted."""
    pass


def _encrypt_dpapi(data: bytes) -> bytes:
    """Encrypt bytes using Windows DPAPI."""
    try:
        import win32crypt
        return win32crypt.CryptProtectData(data, "BillApp", None, None, None, 0)
    except ImportError:
        pass

    # Fallback to ctypes
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_buf = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()

    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "BillApp",
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        error_code = kernel32.GetLastError()
        raise SecretDecryptionError(f"CryptProtectData failed with error code {error_code}")

    try:
        cipher_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return cipher_bytes
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _decrypt_dpapi(cipher_bytes: bytes) -> bytes:
    """Decrypt bytes using Windows DPAPI."""
    try:
        import win32crypt
        _, data = win32crypt.CryptUnprotectData(cipher_bytes, None, None, None, 0)
        return data
    except ImportError:
        pass
    except Exception as exc:
        raise SecretDecryptionError(f"DPAPI decryption failed: {exc}") from exc

    # Fallback to ctypes
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_buf = ctypes.create_string_buffer(cipher_bytes)
    in_blob = DATA_BLOB(len(cipher_bytes), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        error_code = kernel32.GetLastError()
        raise SecretDecryptionError(f"CryptUnprotectData failed with error code {error_code}")

    try:
        plain_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return plain_bytes
    finally:
        kernel32.LocalFree(out_blob.pbData)


def encrypt_secret(plaintext: str, force_b64: bool = False) -> str:
    """Encrypt a secret string for safe local persistence."""
    if not plaintext:
        return ""

    data = plaintext.encode("utf-8")

    if sys.platform == "win32" and not force_b64:
        try:
            cipher = _encrypt_dpapi(data)
            return DPAPI_PREFIX + base64.b64encode(cipher).decode("ascii")
        except Exception as exc:
            logger.warning("DPAPI encryption failed (%s); falling back to base64 tagging", exc)

    # Cross-platform or test fallback
    return B64_PREFIX + base64.b64encode(data).decode("ascii")


def decrypt_secret(stored_value: str) -> str:
    """Decrypt a stored secret string.
    
    Raises SecretDecryptionError if encrypted data is corrupted or from another user/machine.
    """
    if not stored_value:
        return ""

    if stored_value.startswith(DPAPI_PREFIX):
        b64_payload = stored_value[len(DPAPI_PREFIX):]
        try:
            cipher = base64.b64decode(b64_payload)
            plain_bytes = _decrypt_dpapi(cipher)
            return plain_bytes.decode("utf-8")
        except SecretDecryptionError:
            raise
        except Exception as exc:
            raise SecretDecryptionError(f"Failed to decrypt DPAPI secret: {exc}") from exc

    if stored_value.startswith(B64_PREFIX):
        b64_payload = stored_value[len(B64_PREFIX):]
        try:
            plain_bytes = base64.b64decode(b64_payload)
            return plain_bytes.decode("utf-8")
        except Exception as exc:
            raise SecretDecryptionError(f"Failed to decode base64 secret: {exc}") from exc

    # Untagged legacy/plaintext fallback
    return stored_value


def is_encrypted_secret(stored_value: str) -> bool:
    """Check if stored secret is in encrypted/tagged format."""
    return bool(stored_value and (stored_value.startswith(DPAPI_PREFIX) or stored_value.startswith(B64_PREFIX)))


def mask_secret(plaintext: str) -> str:
    """Generate display mask for secret (e.g. sk-****abcd or ****1234)."""
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "****"
    if plaintext.startswith("sk-") and len(plaintext) > 8:
        return "sk-****" + plaintext[-4:]
    return "****" + plaintext[-4:]
