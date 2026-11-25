"""Cross-platform local secret encryption and masking for LLM API keys.

Protection hierarchy (no silent plaintext Base64 downgrade):
1. ``BILLAPP_MASTER_KEY`` environment variable (supplied by Electron main process via
   OS-backed ``safeStorage`` on Windows/macOS/Linux) -> authenticated ``encv1:``
   (SHA-256 CTR + HMAC-SHA256 Encrypt-then-MAC).
2. Windows standalone Python -> Windows DPAPI (``dpapi:``) with strict Win32
   ``ctypes`` type declarations and zeroing of decrypted buffers before ``LocalFree``.
   DPAPI failures raise ``SecretStorageError`` instead of downgrading to ``b64:``.
3. macOS standalone Python -> authenticated ``encv1:`` backed by macOS Keychain
   (``/usr/bin/security``).
4. Linux standalone Python -> authenticated ``encv1:`` backed by Secret Service
   (``secret-tool``) when available.
5. Headless / CI fallback -> authenticated ``encv1:`` backed by a separate 0600
   local key file outside ``bill.db`` so database copies never contain plaintext keys.

Legacy ``b64:`` records remain readable by ``decrypt_secret`` for smooth migration
of older databases, but new writes never emit ``b64:``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from typing import Dict, Optional, Tuple

from billapp.paths import get_appdata_dir

logger = logging.getLogger(__name__)

_DPAPI_PREFIX = "dpapi:"
_ENCV1_PREFIX = "encv1:"
_LEGACY_B64_PREFIX = "b64:"
DPAPI_PREFIX = _DPAPI_PREFIX
ENCV1_PREFIX = _ENCV1_PREFIX
B64_PREFIX = _LEGACY_B64_PREFIX
_KEYCHAIN_SERVICE = "BillApp"
_KEYCHAIN_ACCOUNT = "master_key"

_STORAGE_LABELS: Dict[str, str] = {
    "electron_safe_storage": "系统安全凭据库 (Electron safeStorage)",
    "windows_dpapi": "Windows DPAPI 系统加密",
    "macos_keychain": "macOS 钥匙串 (Keychain)",
    "linux_secret_service": "Linux 系统密钥环 (Secret Service)",
    "local_keyfile": "本机独立密钥文件加密 (权限 0600)",
}


class SecretStorageError(RuntimeError):
    """Raised when secure local credential storage fails to encrypt a secret."""


class SecretDecryptionError(Exception):
    """Raised when an encrypted secret cannot be decrypted."""


# ---------------------------------------------------------------------------
# Pure-stdlib Authenticated Encryption (encv1: SHA-256 CTR + HMAC-SHA256)
# ---------------------------------------------------------------------------

def _derive_subkeys(master_key: bytes, nonce: bytes) -> Tuple[bytes, bytes]:
    enc_key = hmac.new(master_key, b"billapp-encv1-enc:" + nonce, hashlib.sha256).digest()
    mac_key = hmac.new(master_key, b"billapp-encv1-mac:" + nonce, hashlib.sha256).digest()
    return enc_key, mac_key


def _ctr_keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    produced = 0
    counter = 0
    while produced < length:
        block = hashlib.sha256(enc_key + nonce + counter.to_bytes(4, "big")).digest()
        blocks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(blocks)[:length]


def _encv1_encrypt(plaintext_bytes: bytes, master_key: bytes) -> str:
    nonce = os.urandom(16)
    enc_key, mac_key = _derive_subkeys(master_key, nonce)
    stream = _ctr_keystream(enc_key, nonce, len(plaintext_bytes))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext_bytes, stream))
    tag = hmac.new(mac_key, b"encv1:" + nonce + ciphertext, hashlib.sha256).digest()
    payload = base64.b64encode(nonce + ciphertext + tag).decode("ascii")
    return f"{_ENCV1_PREFIX}{payload}"


def _encv1_decrypt(payload_b64: str, master_key: bytes) -> bytes:
    try:
        raw = base64.b64decode(payload_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise SecretDecryptionError(f"Malformed encv1 payload: {exc}") from exc

    if len(raw) < 48:  # 16-byte nonce + 32-byte HMAC tag
        raise SecretDecryptionError("Truncated encv1 secret payload")

    nonce = raw[:16]
    tag = raw[-32:]
    ciphertext = raw[16:-32]
    enc_key, mac_key = _derive_subkeys(master_key, nonce)
    expected_tag = hmac.new(mac_key, b"encv1:" + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_tag, tag):
        raise SecretDecryptionError("Secret authentication tag verification failed (wrong key or tampered data)")

    stream = _ctr_keystream(enc_key, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


# ---------------------------------------------------------------------------
# Cross-Platform Master Key Backends
# ---------------------------------------------------------------------------

def _decode_master_key_material(raw_text: str) -> bytes:
    cleaned = raw_text.strip()
    if not cleaned:
        raise SecretStorageError("Master key material is empty")
    try:
        decoded = bytes.fromhex(cleaned)
        if len(decoded) >= 32:
            return decoded[:32]
    except ValueError:
        pass
    return hashlib.sha256(cleaned.encode("utf-8")).digest()


def _macos_keychain_get_or_create(create: bool = True) -> Optional[bytes]:
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/security"):
        return None
    try:
        res = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                _KEYCHAIN_SERVICE,
                "-a",
                _KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return _decode_master_key_material(res.stdout)
        if not create:
            return None
        new_hex = secrets.token_hex(32)
        # Pass the secret via interactive stdin (`security -i`) so it never appears in process argv.
        interactive_cmd = (
            f'add-generic-password -U -s "{_KEYCHAIN_SERVICE}" -a "{_KEYCHAIN_ACCOUNT}" -w "{new_hex}"\n'
        )
        add_res = subprocess.run(
            ["/usr/bin/security", "-i"],
            input=interactive_cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        # `security -i` exits with code 0 even when subcommands fail; verify stderr and read back key.
        if add_res.returncode == 0 and not (add_res.stderr or "").strip():
            verify_res = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    _KEYCHAIN_SERVICE,
                    "-a",
                    _KEYCHAIN_ACCOUNT,
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if verify_res.returncode == 0 and verify_res.stdout.strip() == new_hex:
                return bytes.fromhex(new_hex)
    except Exception as exc:
        logger.warning("macOS Keychain access unavailable: %s", exc)
    return None


def _linux_secret_service_get_or_create(create: bool = True) -> Optional[bytes]:
    if not sys.platform.startswith("linux"):
        return None
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return None
    secret_tool = shutil.which("secret-tool")
    if not secret_tool:
        return None
    try:
        res = subprocess.run(
            [secret_tool, "lookup", "service", _KEYCHAIN_SERVICE, "account", _KEYCHAIN_ACCOUNT],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return _decode_master_key_material(res.stdout)
        if not create:
            return None
        new_hex = secrets.token_hex(32)
        store_res = subprocess.run(
            [
                secret_tool,
                "store",
                "--label=BillApp Master Key",
                "service",
                _KEYCHAIN_SERVICE,
                "account",
                _KEYCHAIN_ACCOUNT,
            ],
            input=new_hex,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if store_res.returncode == 0:
            return bytes.fromhex(new_hex)
    except Exception as exc:
        logger.warning("Linux Secret Service access unavailable: %s", exc)
    return None


def _local_keyfile_path() -> Path:
    return get_appdata_dir() / ".secret_key"


def _local_keyfile_get_or_create(create: bool = True) -> Optional[bytes]:
    key_path = _local_keyfile_path()
    try:
        if key_path.exists():
            raw = key_path.read_text(encoding="ascii").strip()
            if raw:
                return _decode_master_key_material(raw)
        if not create:
            return None
        key_path.parent.mkdir(parents=True, exist_ok=True)
        new_hex = secrets.token_hex(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(str(key_path), flags, 0o600)
        except FileExistsError:
            raw = key_path.read_text(encoding="ascii").strip()
            if raw:
                return _decode_master_key_material(raw)
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, new_hex.encode("ascii"))
        finally:
            os.close(fd)
        if os.name != "nt":
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        return bytes.fromhex(new_hex)
    except Exception as exc:
        raise SecretStorageError(f"Failed to access local secret keyfile: {exc}") from exc


def _resolve_master_key(create: bool = True) -> Tuple[bytes, str]:
    env_key = os.environ.get("BILLAPP_MASTER_KEY", "").strip()
    if env_key:
        backend = os.environ.get("BILLAPP_SECRET_BACKEND", "").strip() or "electron_safe_storage"
        return _decode_master_key_material(env_key), backend

    if sys.platform == "darwin":
        key = _macos_keychain_get_or_create(create=create)
        if key is not None:
            return key, "macos_keychain"

    if sys.platform.startswith("linux"):
        key = _linux_secret_service_get_or_create(create=create)
        if key is not None:
            return key, "linux_secret_service"

    key = _local_keyfile_get_or_create(create=create)
    if key is None:
        raise SecretDecryptionError("Local master key is not initialized")
    return key, "local_keyfile"


def get_secret_storage_info() -> Dict[str, str]:
    """Return active local credential protection backend id and user-facing label."""
    if os.environ.get("BILLAPP_MASTER_KEY", "").strip():
        backend = os.environ.get("BILLAPP_SECRET_BACKEND", "").strip() or "electron_safe_storage"
        return {"backend": backend, "label": _STORAGE_LABELS.get(backend, _STORAGE_LABELS["electron_safe_storage"])}

    if sys.platform == "win32":
        return {"backend": "windows_dpapi", "label": _STORAGE_LABELS["windows_dpapi"]}

    if sys.platform == "darwin" and os.path.exists("/usr/bin/security"):
        return {"backend": "macos_keychain", "label": _STORAGE_LABELS["macos_keychain"]}

    if (
        sys.platform.startswith("linux")
        and os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        and shutil.which("secret-tool")
    ):
        return {"backend": "linux_secret_service", "label": _STORAGE_LABELS["linux_secret_service"]}

    return {"backend": "local_keyfile", "label": _STORAGE_LABELS["local_keyfile"]}


# ---------------------------------------------------------------------------
# Windows DPAPI Implementation (pywin32 or hardened ctypes fallback)
# ---------------------------------------------------------------------------

def _dpapi_encrypt(plaintext_bytes: bytes) -> bytes:
    """Encrypt bytes using Windows DPAPI (CurrentUser scope)."""
    try:
        import win32crypt  # type: ignore
        return win32crypt.CryptProtectData(plaintext_bytes, "BillApp-LLM-Secret", None, None, None, 0)
    except ImportError:
        pass

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    buf = ctypes.create_string_buffer(plaintext_bytes, len(plaintext_bytes))
    in_blob = DATA_BLOB(len(plaintext_bytes), buf)
    out_blob = DATA_BLOB()

    try:
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "BillApp-LLM-Secret",
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise SecretStorageError(f"CryptProtectData failed with error code {ctypes.GetLastError()}")

        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            if out_blob.pbData:
                kernel32.LocalFree(out_blob.pbData)
    finally:
        ctypes.memset(ctypes.addressof(buf), 0, len(plaintext_bytes))


def _dpapi_decrypt(cipher_bytes: bytes) -> bytes:
    """Decrypt bytes using Windows DPAPI (CurrentUser scope), wiping decrypted memory before free."""
    try:
        import win32crypt  # type: ignore
        _, decrypted = win32crypt.CryptUnprotectData(cipher_bytes, None, None, None, 0)
        return decrypted
    except ImportError:
        pass

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    buf = ctypes.create_string_buffer(cipher_bytes, len(cipher_bytes))
    in_blob = DATA_BLOB(len(cipher_bytes), buf)
    out_blob = DATA_BLOB()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretDecryptionError(f"CryptUnprotectData failed with error code {ctypes.GetLastError()}")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            if out_blob.cbData > 0:
                ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
            kernel32.LocalFree(out_blob.pbData)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext API key using the active local cryptographic backend.

    Never silently downgrades to plaintext ``b64:`` encoding. Raises
    ``SecretStorageError`` if encryption fails.
    """
    if not plaintext:
        return ""

    raw_bytes = plaintext.encode("utf-8")

    # 1. Prefer Electron safeStorage-backed master key when provided
    if os.environ.get("BILLAPP_MASTER_KEY", "").strip():
        master_key, _ = _resolve_master_key(create=True)
        return _encv1_encrypt(raw_bytes, master_key)

    # 2. Windows standalone: use DPAPI strictly without silent b64 downgrade
    if sys.platform == "win32":
        try:
            cipher = _dpapi_encrypt(raw_bytes)
            b64_cipher = base64.b64encode(cipher).decode("ascii")
            return f"{_DPAPI_PREFIX}{b64_cipher}"
        except SecretStorageError:
            raise
        except Exception as exc:
            logger.error("Windows DPAPI encryption failed: %s", exc)
            raise SecretStorageError(f"Windows DPAPI 加密失败，已拒绝以明文存储密钥: {exc}") from exc

    # 3. macOS / Linux: OS keychain / Secret Service / 0600 local keyfile
    master_key, _ = _resolve_master_key(create=True)
    return _encv1_encrypt(raw_bytes, master_key)


def decrypt_secret(stored: str) -> str:
    """Decrypt a stored secret string back into plaintext."""
    if not stored:
        return ""

    if stored.startswith(_ENCV1_PREFIX):
        payload = stored[len(_ENCV1_PREFIX) :]
        try:
            master_key, _ = _resolve_master_key(create=False)
            pt_bytes = _encv1_decrypt(payload, master_key)
            return pt_bytes.decode("utf-8")
        except SecretDecryptionError:
            raise
        except Exception as exc:
            raise SecretDecryptionError(f"Failed to decrypt encv1 secret: {exc}") from exc

    if stored.startswith(_DPAPI_PREFIX):
        if sys.platform != "win32":
            raise SecretDecryptionError("Cannot decrypt Windows DPAPI secret on non-Windows platform.")
        b64_cipher = stored[len(_DPAPI_PREFIX) :]
        try:
            cipher = base64.b64decode(b64_cipher.encode("ascii"), validate=True)
            pt_bytes = _dpapi_decrypt(cipher)
            return pt_bytes.decode("utf-8")
        except SecretDecryptionError:
            raise
        except Exception as exc:
            raise SecretDecryptionError(f"DPAPI decryption failed: {exc}") from exc

    if stored.startswith(_LEGACY_B64_PREFIX):
        b64_pt = stored[len(_LEGACY_B64_PREFIX) :]
        try:
            pt_bytes = base64.b64decode(b64_pt.encode("ascii"), validate=True)
            return pt_bytes.decode("utf-8")
        except Exception as exc:
            raise SecretDecryptionError(f"Legacy Base64 decoding failed: {exc}") from exc

    # Legacy unprefixed plaintext fallback
    return stored


def mask_secret(plaintext: str) -> str:
    """Mask an API key for UI display (e.g., 'sk-proj-12345678abcd' -> 'sk-****abcd')."""
    if not plaintext:
        return ""
    text = plaintext.strip()
    if len(text) <= 8:
        return "****"
    if text.startswith("sk-"):
        return f"sk-****{text[-4:]}"
    return f"****{text[-4:]}"


def is_encrypted_secret(value: Optional[str]) -> bool:
    """Check whether a string carries a known secret envelope prefix."""
    if not value:
        return False
    return (
        value.startswith(_ENCV1_PREFIX)
        or value.startswith(_DPAPI_PREFIX)
        or value.startswith(_LEGACY_B64_PREFIX)
    )
