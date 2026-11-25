"""Universal OpenAI-compatible LLM client via httpx.

Features:
- Compatible with DeepSeek, Qwen/DashScope, Moonshot/Kimi, OpenAI, Ollama, vLLM
- Exponential backoff retry on transient errors [1.0, 2.0, 4.0]
- Dual-mode JSON extraction (response_format=json_object + markdown/regex fallback)
- SQLite SHA-256 caching via llm_cache table
- Token usage tracking and health check testing
- Hermetic test support via httpx.MockTransport
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import httpx

from billapp.core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

_BLOCKED_LLM_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
    "instance-data",
    "instance-data.ec2.internal",
}


def normalize_url_origin(url: str) -> Tuple[str, str, int]:
    """Return canonical (scheme, lowercase_host, effective_port) for origin comparison."""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if parsed.port is not None:
        port = parsed.port
    elif scheme == "https":
        port = 443
    elif scheme == "http":
        port = 80
    else:
        port = 0
    return scheme, host, port


_NON_CANONICAL_IPV4_RE = re.compile(
    r"^(?:0x[0-9a-f]+|0[0-7]*|\d+)(?:\.(?:0x[0-9a-f]+|0[0-7]*|\d+)){0,3}$",
    re.IGNORECASE,
)


def _parse_literal_ip(host: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """Parse canonical or alternate-radix/short-form literal IP addresses without DNS."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        if _NON_CANONICAL_IPV4_RE.match(host):
            try:
                packed = socket.inet_aton(host)
                ip = ipaddress.IPv4Address(packed)
            except OSError as exc:
                raise ValueError(f"已拦截非法或非标准 IP 地址格式: {host}") from exc
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def validate_llm_base_url(base_url: str) -> str:
    """Validate LLM Base URL against scheme, userinfo, and metadata/link-local SSRF vectors."""
    raw = (base_url or "").strip()
    if not raw:
        raise ValueError("API 服务地址 (Base URL) 不能为空")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError("API 服务地址仅支持 http 或 https 协议")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("API 服务地址不允许包含内嵌认证凭据 (@)")

    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("API 服务地址端口无效（必须在 1-65535 之间）") from exc

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("API 服务地址缺少有效主机名")

    if host in _BLOCKED_LLM_HOSTS:
        raise ValueError(f"已拦截禁止访问的元数据主机地址: {host}")

    # Validate literal IP addresses (allow loopback for local Ollama/vLLM; block link-local/IMDS/unspecified)
    ip = _parse_literal_ip(host)

    if ip is not None:
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise ValueError(f"已拦截禁止访问的链路本地或非法 IP 地址: {host}")
        # AWS IPv6 IMDS fd00:ec2::254
        if str(ip) == "fd00:ec2::254":
            raise ValueError(f"已拦截禁止访问的云元数据地址: {host}")

    return raw


# =========================================================================
# Configuration Data Model
# =========================================================================

@dataclass
class LLMConfig:
    base_url: str = ""                         # e.g., "https://api.deepseek.com/v1"
    api_key: str = ""                          # Plaintext in-memory API key
    model: str = "deepseek-chat"               # Model identifier
    temperature: float = 0.1
    timeout_sec: float = 60.0
    max_retries: int = 2
    enabled: bool = False
    task_classify_enabled: bool = True
    task_review_enabled: bool = True
    task_arbitrate_enabled: bool = True
    task_merchant_norm_enabled: bool = True
    task_suggest_profile_enabled: bool = True
    task_full_parse_enabled: bool = True
    auto_confirm_dedup: bool = False
    auto_confirm_threshold: float = 0.90
    api_key_decryption_failed: bool = False
    raw_api_key_encrypted: str = ""

    @classmethod
    def load_from_db(cls, conn: sqlite3.Connection) -> LLMConfig:
        """Load LLM configuration from SQLite settings table, preserving config even if API key decryption fails."""
        row = conn.execute("SELECT value FROM settings WHERE key = 'llm_config'").fetchone()
        if not row:
            return cls()
        try:
            data = json.loads(row[0])
            if not isinstance(data, dict):
                return cls()
        except Exception as exc:
            logger.warning("Failed to parse llm_config JSON from settings: %s", exc)
            return cls()

        api_key_enc = str(data.get("api_key_encrypted", "") or "")
        plain_key = ""
        decryption_failed = False
        if api_key_enc:
            try:
                plain_key = decrypt_secret(api_key_enc)
            except SecretDecryptionError as exc:
                logger.warning("Failed to decrypt saved LLM API key (preserving other config): %s", exc)
                plain_key = ""
                decryption_failed = True
        elif data.get("api_key"):
            plain_key = str(data.get("api_key", ""))

        try:
            return cls(
                base_url=str(data.get("base_url", "") or ""),
                api_key=plain_key,
                model=str(data.get("model", "deepseek-chat") or "deepseek-chat"),
                temperature=float(data.get("temperature", 0.1)),
                timeout_sec=float(data.get("timeout_sec", 60.0)),
                max_retries=int(data.get("max_retries", 2)),
                enabled=bool(data.get("enabled", False)),
                task_classify_enabled=bool(data.get("task_classify_enabled", True)),
                task_review_enabled=bool(data.get("task_review_enabled", True)),
                task_arbitrate_enabled=bool(data.get("task_arbitrate_enabled", True)),
                task_merchant_norm_enabled=bool(data.get("task_merchant_norm_enabled", True)),
                task_suggest_profile_enabled=bool(data.get("task_suggest_profile_enabled", True)),
                task_full_parse_enabled=bool(data.get("task_full_parse_enabled", True)),
                auto_confirm_dedup=bool(data.get("auto_confirm_dedup", False)),
                auto_confirm_threshold=float(data.get("auto_confirm_threshold", 0.90)),
                api_key_decryption_failed=decryption_failed,
                raw_api_key_encrypted=api_key_enc,
            )
        except Exception as exc:
            logger.warning("Failed to construct LLMConfig from settings: %s", exc)
            return cls()

    def save_to_db(self, conn: sqlite3.Connection) -> None:
        """Persist LLM configuration to SQLite settings table with encrypted API key."""
        if self.api_key:
            enc_key = encrypt_secret(self.api_key)
        elif self.api_key_decryption_failed and self.raw_api_key_encrypted:
            enc_key = self.raw_api_key_encrypted
        else:
            enc_key = ""
        payload = {
            "base_url": self.base_url,
            "api_key_encrypted": enc_key,
            "model": self.model,
            "temperature": self.temperature,
            "timeout_sec": self.timeout_sec,
            "max_retries": self.max_retries,
            "enabled": self.enabled,
            "task_classify_enabled": self.task_classify_enabled,
            "task_review_enabled": self.task_review_enabled,
            "task_arbitrate_enabled": self.task_arbitrate_enabled,
            "task_merchant_norm_enabled": self.task_merchant_norm_enabled,
            "task_suggest_profile_enabled": self.task_suggest_profile_enabled,
            "task_full_parse_enabled": self.task_full_parse_enabled,
            "auto_confirm_dedup": self.auto_confirm_dedup,
            "auto_confirm_threshold": self.auto_confirm_threshold,
        }
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ('llm_config', ?, datetime('now', 'localtime'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (json.dumps(payload, ensure_ascii=False),),
        )
        conn.commit()


# =========================================================================
# Robust JSON Extractor
# =========================================================================

def _extract_delta_text(raw_val: Any) -> str:
    """Extract raw string fragment from a streaming delta field without stripping whitespace."""
    if raw_val is None:
        return ""
    if isinstance(raw_val, str):
        return raw_val
    if isinstance(raw_val, list):
        parts: List[str] = []
        for item in raw_val:
            if isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(raw_val)


def _parse_sse_line(raw_line: str) -> Tuple[str, str, Optional[Dict[str, Any]], bool]:
    """Parse one SSE line into (content_delta, reasoning_delta, usage_dict, is_done)."""
    line = raw_line.strip()
    if not line or line.startswith(":"):
        return "", "", None, False
    if not line.startswith("data:"):
        return "", "", None, False
    data_str = line[5:].strip()
    if not data_str:
        return "", "", None, False
    if data_str == "[DONE]":
        return "", "", None, True
    try:
        chunk = json.loads(data_str)
    except Exception:
        return "", "", None, False
    if not isinstance(chunk, dict):
        return "", "", None, False
    if chunk.get("error") and not chunk.get("choices"):
        raise RuntimeError(f"LLM stream error: {chunk['error']}")
    usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else None
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta") or first.get("message") or {}
        if isinstance(delta, dict):
            content_delta = _extract_delta_text(delta.get("content"))
            reasoning_delta = _extract_delta_text(
                delta.get("reasoning_content") if "reasoning_content" in delta else delta.get("reasoning")
            )
            return content_delta, reasoning_delta, usage, False
    return "", "", usage, False


def _extract_json_response_content(data: Any) -> Tuple[str, int, int]:
    """Extract normalized text content and token usage from a non-streaming JSON response."""
    if isinstance(data, dict) and "choices" in data and data["choices"]:
        choice = data["choices"][0]
        msg = choice.get("message", {}) if isinstance(choice, dict) else {}
        raw_content = msg.get("content", "") if isinstance(msg, dict) else ""
        content = _normalize_message_content(raw_content)
        if not content and isinstance(msg, dict):
            fallback_reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            content = _normalize_message_content(fallback_reasoning)
    elif isinstance(data, dict) and "content" in data and isinstance(data["content"], (str, list)):
        content = _normalize_message_content(data["content"])
    else:
        content = json.dumps(data, ensure_ascii=False)

    usage = data.get("usage", {}) if isinstance(data, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    p_tok = int(usage.get("prompt_tokens", 0) or 0)
    c_tok = int(usage.get("completion_tokens", 0) or 0)
    return content, p_tok, c_tok


def _resolve_stream_wall_limit(timeout_sec: float) -> float:
    """Overall wall-clock safety ceiling for an active SSE stream (prevents infinite loops)."""
    t = max(float(timeout_sec or 60.0), 1.0)
    return min(max(t * 2.5, t + 30.0), 480.0)


def _normalize_message_content(raw_content: Any) -> str:
    """Normalize OpenAI-compatible message content and strip reasoning tags."""
    if raw_content is None:
        return ""
    if isinstance(raw_content, list):
        parts: List[str] = []
        for item in raw_content:
            if isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(item, str):
                parts.append(item)
        text = "\n".join(parts)
    else:
        text = str(raw_content)

    # Strip <think>...</think> blocks emitted by reasoning models so JSON scanning won't match inside thoughts
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    return text.strip()


def extract_json(raw_text: str) -> Union[Dict[str, Any], List[Any]]:
    """Extract valid JSON object or array from LLM response text.
    
    Tries multiple strategies:
    1. Direct parse
    2. Markdown code fences (```json ... ```)
    3. Outer bracket scanning
    4. Trailing comma scrubbing
    """
    clean = _normalize_message_content(raw_text)
    if not clean:
        raise ValueError("LLM returned empty response content.")
    # Strategy 1: Direct parse
    try:
        return json.loads(clean)
    except Exception:
        pass

    # Strategy 2: Markdown code fence extraction
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean, re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            clean = candidate

    # Strategy 3: Scan for outer brackets
    first_brace = clean.find("{")
    last_brace = clean.rfind("}")
    first_bracket = clean.find("[")
    last_bracket = clean.rfind("]")

    start_idx = -1
    end_idx = -1

    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        start_idx = first_brace
        end_idx = last_brace
    elif first_bracket != -1:
        start_idx = first_bracket
        end_idx = last_bracket

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        slice_text = clean[start_idx : end_idx + 1]
        try:
            return json.loads(slice_text)
        except Exception:
            # Strategy 4: Scrub trailing commas before closing braces/brackets
            scrubbed = re.sub(r",\s*([}\]])", r"\1", slice_text)
            try:
                return json.loads(scrubbed)
            except Exception:
                pass

    raise ValueError(f"Could not extract JSON from LLM response:\n{raw_text[:200]}")


# =========================================================================
# Universal LLM Client
# =========================================================================

class LLMClient:
    """Universal OpenAI-compatible LLM client with retry, caching, and offline safety."""

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        conn: Optional[sqlite3.Connection] = None,
        transport: Optional[httpx.BaseTransport] = None,
        auto_commit: bool = True,
    ):
        self.config = config or LLMConfig()
        self.conn = conn
        self.transport = transport
        self.auto_commit = auto_commit
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_requests: int = 0
        self.cache_hits: int = 0

    def is_available(self) -> bool:
        """Check if LLM is enabled and configured with base URL."""
        return bool(self.config.enabled and self.config.base_url.strip())

    @staticmethod
    def normalize_url(base_url: str) -> str:
        """Normalize API URL to OpenAI-compatible chat completions endpoint."""
        url = base_url.strip().rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/v1"):
            return f"{url}/chat/completions"
        return f"{url}/v1/chat/completions"

    @staticmethod
    def normalize_models_url(base_url: str) -> str:
        """Normalize API URL to OpenAI-compatible models listing endpoint."""
        url = base_url.strip().rstrip("/")
        if url.endswith("/models"):
            return url
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")].rstrip("/")
        if url.endswith("/v1"):
            return f"{url}/models"
        return f"{url}/v1/models"

    def compute_cache_key(self, task_type: str, model: str, temperature: float, messages: List[Dict[str, Any]]) -> str:
        """Generate deterministic SHA-256 hash for cache lookup."""
        envelope = {
            "task_type": task_type,
            "model": model,
            "temperature": round(temperature, 2),
            "messages": messages,
        }
        serialized = json.dumps(envelope, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get_from_cache(self, input_hash: str) -> Optional[Tuple[Union[Dict[str, Any], List[Any]], int, int]]:
        """Look up response from sqlite llm_cache."""
        if not self.conn:
            return None
        try:
            row = self.conn.execute(
                "SELECT response_json, prompt_tokens, completion_tokens FROM llm_cache WHERE input_hash = ?",
                (input_hash,),
            ).fetchone()
            if row:
                self.cache_hits += 1
                return json.loads(row[0]), row[1] or 0, row[2] or 0
        except Exception as exc:
            logger.debug("llm_cache read error: %s", exc)
        return None

    def save_to_cache(
        self,
        task_type: str,
        input_hash: str,
        response_data: Union[Dict[str, Any], List[Any]],
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Store LLM response into sqlite llm_cache."""
        if not self.conn:
            return
        try:
            resp_str = json.dumps(response_data, ensure_ascii=False)
            self.conn.execute(
                """
                INSERT INTO llm_cache (task_type, input_hash, response_json, model, prompt_tokens, completion_tokens)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(input_hash) DO UPDATE SET
                    response_json = excluded.response_json,
                    model = excluded.model,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens
                """,
                (task_type, input_hash, resp_str, model, prompt_tokens, completion_tokens),
            )
            if self.auto_commit:
                self.conn.commit()
        except Exception as exc:
            logger.debug("llm_cache write error: %s", exc)

    async def chat_completion_async(
        self,
        task_type: str,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        use_json_mode: bool = True,
        use_cache: bool = True,
        retry_on_timeout: bool = True,
        use_stream: bool = True,
    ) -> Union[Dict[str, Any], List[Any]]:
        """Asynchronously call chat completion with SSE streaming, caching, and backoff."""
        if not self.is_available():
            raise RuntimeError("LLM is disabled or not configured.")

        temp = temperature if temperature is not None else self.config.temperature
        model = self.config.model
        input_hash = self.compute_cache_key(task_type, model, temp, messages)

        # Check Cache
        if use_cache:
            cached = self.get_from_cache(input_hash)
            if cached is not None:
                data, p_tok, c_tok = cached
                self.total_prompt_tokens += p_tok
                self.total_completion_tokens += c_tok
                return data

        validate_llm_base_url(self.config.base_url)
        url = self.normalize_url(self.config.base_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key.strip()}",
        }

        effective_stream = bool(use_stream)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        if effective_stream:
            payload["stream"] = True

        backoff_delays = [1.0, 2.0, 4.0]
        attempts = 0
        max_attempts = max(1, int(self.config.max_retries) + 1)
        last_error: Optional[Exception] = None
        stream_wall_limit = _resolve_stream_wall_limit(self.config.timeout_sec)

        req_timeout = httpx.Timeout(self.config.timeout_sec, connect=min(float(self.config.timeout_sec), 10.0))
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=req_timeout,
            follow_redirects=False,
        ) as client:
            while attempts <= self.config.max_retries:
                attempts += 1
                attempt_start = time.perf_counter()
                try:
                    self.total_requests += 1
                    async with client.stream("POST", url, headers=headers, json=payload) as resp:
                        # Handle models rejecting response_format or stream (HTTP 400)
                        if resp.status_code == 400 and (use_json_mode or effective_stream):
                            await resp.aread()
                            if use_json_mode:
                                logger.debug("Server rejected JSON mode (HTTP 400); retrying without response_format")
                                payload.pop("response_format", None)
                                use_json_mode = False
                            else:
                                logger.debug("Server rejected SSE stream (HTTP 400); retrying without stream")
                                payload.pop("stream", None)
                                effective_stream = False
                            attempts -= 1
                            continue

                        # Hard auth errors do not retry
                        if resp.status_code in (401, 403):
                            await resp.aread()
                            raise httpx.HTTPStatusError(
                                f"LLM Authentication failed (HTTP {resp.status_code}): {resp.text[:100]}",
                                request=resp.request,
                                response=resp,
                            )

                        if resp.status_code >= 400:
                            await resp.aread()
                            resp.raise_for_status()

                        content_type = (resp.headers.get("content-type") or "").lower()
                        if "text/event-stream" in content_type:
                            content_parts: List[str] = []
                            reasoning_parts: List[str] = []
                            p_tok = 0
                            c_tok = 0
                            async for raw_line in resp.aiter_lines():
                                if (time.perf_counter() - attempt_start) > stream_wall_limit:
                                    raise httpx.ReadTimeout(
                                        f"LLM stream exceeded max duration ({stream_wall_limit:.0f}s)",
                                        request=resp.request,
                                    )
                                c_delta, r_delta, usage_obj, is_done = _parse_sse_line(raw_line)
                                if c_delta:
                                    content_parts.append(c_delta)
                                if r_delta:
                                    reasoning_parts.append(r_delta)
                                if usage_obj:
                                    p_tok = int(usage_obj.get("prompt_tokens", p_tok) or p_tok)
                                    c_tok = int(usage_obj.get("completion_tokens", c_tok) or c_tok)
                                if is_done:
                                    break
                            content = _normalize_message_content("".join(content_parts))
                            if not content and reasoning_parts:
                                content = _normalize_message_content("".join(reasoning_parts))
                        else:
                            await resp.aread()
                            data = resp.json()
                            content, p_tok, c_tok = _extract_json_response_content(data)

                    self.total_prompt_tokens += p_tok
                    self.total_completion_tokens += c_tok

                    # Extract and parse JSON
                    parsed_result = extract_json(content)

                    # Cache result
                    if use_cache:
                        self.save_to_cache(task_type, input_hash, parsed_result, model, p_tok, c_tok)
                    return parsed_result

                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    last_error = exc
                    elapsed_ms = int((time.perf_counter() - attempt_start) * 1000)
                    # Do not retry on 400, 401, 403, 404
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401, 403, 404):
                        raise

                    if isinstance(exc, httpx.TimeoutException) and not retry_on_timeout:
                        logger.warning(
                            "LLM request timed out (task=%s model=%s timeout=%.1fs elapsed=%dms); skipping retry.",
                            task_type,
                            model,
                            float(self.config.timeout_sec),
                            elapsed_ms,
                        )
                        raise

                    if attempts <= self.config.max_retries:
                        delay = backoff_delays[min(attempts - 1, len(backoff_delays) - 1)]
                        logger.warning(
                            "LLM request error (task=%s model=%s attempt=%d/%d timeout=%.1fs elapsed=%dms err=%s). Retrying in %ss...",
                            task_type,
                            model,
                            attempts,
                            max_attempts,
                            float(self.config.timeout_sec),
                            elapsed_ms,
                            exc,
                            delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.warning(
                            "LLM request failed after %d/%d attempts (task=%s model=%s timeout=%.1fs elapsed=%dms err=%s).",
                            attempts,
                            max_attempts,
                            task_type,
                            model,
                            float(self.config.timeout_sec),
                            elapsed_ms,
                            exc,
                        )
                        break

        raise last_error or RuntimeError("LLM request failed after retries.")

    def chat_completion_sync(
        self,
        task_type: str,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        use_json_mode: bool = True,
        use_cache: bool = True,
        retry_on_timeout: bool = True,
        use_stream: bool = True,
    ) -> Union[Dict[str, Any], List[Any]]:
        """Synchronously call chat completion with SSE streaming, caching, and backoff."""
        if not self.is_available():
            raise RuntimeError("LLM is disabled or not configured.")

        temp = temperature if temperature is not None else self.config.temperature
        model = self.config.model
        input_hash = self.compute_cache_key(task_type, model, temp, messages)

        # Check Cache
        if use_cache:
            cached = self.get_from_cache(input_hash)
            if cached is not None:
                data, p_tok, c_tok = cached
                self.total_prompt_tokens += p_tok
                self.total_completion_tokens += c_tok
                return data

        validate_llm_base_url(self.config.base_url)
        url = self.normalize_url(self.config.base_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key.strip()}",
        }

        effective_stream = bool(use_stream)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        if effective_stream:
            payload["stream"] = True

        backoff_delays = [0.05, 0.1, 0.2] if self.transport else [1.0, 2.0, 4.0]
        attempts = 0
        max_attempts = max(1, int(self.config.max_retries) + 1)
        last_error: Optional[Exception] = None
        stream_wall_limit = _resolve_stream_wall_limit(self.config.timeout_sec)

        req_timeout = httpx.Timeout(self.config.timeout_sec, connect=min(float(self.config.timeout_sec), 10.0))
        with httpx.Client(
            transport=self.transport,
            timeout=req_timeout,
            follow_redirects=False,
        ) as client:
            while attempts <= self.config.max_retries:
                attempts += 1
                attempt_start = time.perf_counter()
                try:
                    self.total_requests += 1
                    with client.stream("POST", url, headers=headers, json=payload) as resp:
                        # Handle models rejecting response_format or stream (HTTP 400)
                        if resp.status_code == 400 and (use_json_mode or effective_stream):
                            resp.read()
                            if use_json_mode:
                                logger.debug("Server rejected JSON mode (HTTP 400); retrying without response_format")
                                payload.pop("response_format", None)
                                use_json_mode = False
                            else:
                                logger.debug("Server rejected SSE stream (HTTP 400); retrying without stream")
                                payload.pop("stream", None)
                                effective_stream = False
                            attempts -= 1
                            continue

                        # Hard auth errors do not retry
                        if resp.status_code in (401, 403):
                            resp.read()
                            raise httpx.HTTPStatusError(
                                f"LLM Authentication failed (HTTP {resp.status_code}): {resp.text[:100]}",
                                request=resp.request,
                                response=resp,
                            )

                        if resp.status_code >= 400:
                            resp.read()
                            resp.raise_for_status()

                        content_type = (resp.headers.get("content-type") or "").lower()
                        if "text/event-stream" in content_type:
                            content_parts: List[str] = []
                            reasoning_parts: List[str] = []
                            p_tok = 0
                            c_tok = 0
                            for raw_line in resp.iter_lines():
                                if (time.perf_counter() - attempt_start) > stream_wall_limit:
                                    raise httpx.ReadTimeout(
                                        f"LLM stream exceeded max duration ({stream_wall_limit:.0f}s)",
                                        request=resp.request,
                                    )
                                c_delta, r_delta, usage_obj, is_done = _parse_sse_line(raw_line)
                                if c_delta:
                                    content_parts.append(c_delta)
                                if r_delta:
                                    reasoning_parts.append(r_delta)
                                if usage_obj:
                                    p_tok = int(usage_obj.get("prompt_tokens", p_tok) or p_tok)
                                    c_tok = int(usage_obj.get("completion_tokens", c_tok) or c_tok)
                                if is_done:
                                    break
                            content = _normalize_message_content("".join(content_parts))
                            if not content and reasoning_parts:
                                content = _normalize_message_content("".join(reasoning_parts))
                        else:
                            resp.read()
                            data = resp.json()
                            content, p_tok, c_tok = _extract_json_response_content(data)

                    self.total_prompt_tokens += p_tok
                    self.total_completion_tokens += c_tok

                    # Extract and parse JSON
                    parsed_result = extract_json(content)

                    # Cache result
                    if use_cache:
                        self.save_to_cache(task_type, input_hash, parsed_result, model, p_tok, c_tok)
                    return parsed_result

                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                    last_error = exc
                    elapsed_ms = int((time.perf_counter() - attempt_start) * 1000)
                    # Do not retry on 400, 401, 403, 404
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401, 403, 404):
                        raise

                    if isinstance(exc, httpx.TimeoutException) and not retry_on_timeout:
                        logger.warning(
                            "LLM request timed out (task=%s model=%s timeout=%.1fs elapsed=%dms); skipping retry.",
                            task_type,
                            model,
                            float(self.config.timeout_sec),
                            elapsed_ms,
                        )
                        raise

                    if attempts <= self.config.max_retries:
                        delay = backoff_delays[min(attempts - 1, len(backoff_delays) - 1)]
                        logger.warning(
                            "LLM request error (task=%s model=%s attempt=%d/%d timeout=%.1fs elapsed=%dms err=%s). Retrying in %ss...",
                            task_type,
                            model,
                            attempts,
                            max_attempts,
                            float(self.config.timeout_sec),
                            elapsed_ms,
                            exc,
                            delay,
                        )
                        time.sleep(delay)
                    else:
                        logger.warning(
                            "LLM request failed after %d/%d attempts (task=%s model=%s timeout=%.1fs elapsed=%dms err=%s).",
                            attempts,
                            max_attempts,
                            task_type,
                            model,
                            float(self.config.timeout_sec),
                            elapsed_ms,
                            exc,
                        )
                        break

        raise last_error or RuntimeError("LLM request failed after retries.")

    def test_connection(self) -> Dict[str, Any]:
        """Test API connectivity and return latency and model info."""
        if not self.config.base_url:
            return {"ok": False, "error": "Base URL not set"}

        try:
            validate_llm_base_url(self.config.base_url)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        url = self.normalize_url(self.config.base_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key.strip()}",
        }
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }

        start_time = time.perf_counter()
        timeout = min(max(float(self.config.timeout_sec or 10.0), 1.0), 180.0)
        req_timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))
        try:
            with httpx.Client(transport=self.transport, timeout=req_timeout, follow_redirects=False) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                latency_ms = round((time.perf_counter() - start_time) * 1000)
                data = resp.json()
                ret_model = data.get("model", self.config.model)
                return {
                    "ok": True,
                    "latency_ms": latency_ms,
                    "model": ret_model,
                }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
            }

    def list_models(self) -> Dict[str, Any]:
        """Fetch available model IDs from OpenAI-compatible /models endpoint."""
        if not self.config.base_url.strip():
            return {"ok": False, "models": [], "error": "Base URL not set"}

        try:
            validate_llm_base_url(self.config.base_url)
        except ValueError as exc:
            return {"ok": False, "models": [], "error": str(exc)}

        url = self.normalize_models_url(self.config.base_url)
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"

        timeout = min(max(float(self.config.timeout_sec or 10.0), 1.0), 180.0)
        req_timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))
        try:
            with httpx.Client(transport=self.transport, timeout=req_timeout, follow_redirects=False) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                payload = resp.json()

            raw_items: Any = []
            if isinstance(payload, dict):
                raw_items = payload.get("data") or payload.get("models") or []
            elif isinstance(payload, list):
                raw_items = payload

            models: List[Dict[str, str]] = []
            seen: set[str] = set()
            if isinstance(raw_items, list):
                for item in raw_items:
                    if isinstance(item, str):
                        model_id = item.strip()
                        name = model_id
                        owned_by = ""
                        desc = ""
                    elif isinstance(item, dict):
                        model_id = str(item.get("id") or item.get("name") or "").strip()
                        name = str(item.get("name") or model_id).strip()
                        owned_by = str(item.get("owned_by") or item.get("owner") or "").strip()
                        desc = str(item.get("description") or "").strip()
                    else:
                        continue
                    if not model_id or model_id in seen:
                        continue
                    seen.add(model_id)
                    models.append(
                        {
                            "id": model_id,
                            "name": name,
                            "owned_by": owned_by,
                            "description": desc,
                        }
                    )

            return {"ok": True, "models": models, "error": None}
        except Exception as exc:
            return {"ok": False, "models": [], "error": str(exc)}
