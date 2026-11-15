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
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

from billapp.core.secrets import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


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

    @classmethod
    def load_from_db(cls, conn: sqlite3.Connection) -> LLMConfig:
        """Load LLM configuration from SQLite settings table."""
        row = conn.execute("SELECT value FROM settings WHERE key = 'llm_config'").fetchone()
        if not row:
            return cls()
        try:
            data = json.loads(row[0])
            api_key_enc = data.get("api_key_encrypted", "")
            plain_key = decrypt_secret(api_key_enc) if api_key_enc else data.get("api_key", "")
            return cls(
                base_url=data.get("base_url", ""),
                api_key=plain_key,
                model=data.get("model", "deepseek-chat"),
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
            )
        except Exception as exc:
            logger.warning("Failed to parse llm_config from settings: %s", exc)
            return cls()

    def save_to_db(self, conn: sqlite3.Connection) -> None:
        """Persist LLM configuration to SQLite settings table with encrypted API key."""
        enc_key = encrypt_secret(self.api_key) if self.api_key else ""
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
    ):
        self.config = config or LLMConfig()
        self.conn = conn
        self.transport = transport
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
    ) -> Union[Dict[str, Any], List[Any]]:
        """Asynchronously call chat completion with caching and backoff."""
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

        url = self.normalize_url(self.config.base_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key.strip()}",
        }

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        backoff_delays = [1.0, 2.0, 4.0]
        attempts = 0
        last_error: Optional[Exception] = None

        req_timeout = httpx.Timeout(self.config.timeout_sec, connect=min(float(self.config.timeout_sec), 10.0))
        async with httpx.AsyncClient(transport=self.transport, timeout=req_timeout) as client:
            while attempts <= self.config.max_retries:
                attempts += 1
                try:
                    self.total_requests += 1
                    resp = await client.post(url, headers=headers, json=payload)

                    # Handle models rejecting response_format (400 error)
                    if resp.status_code == 400 and use_json_mode:
                        logger.debug("Server rejected JSON mode (HTTP 400); retrying without response_format")
                        payload.pop("response_format", None)
                        use_json_mode = False
                        continue

                    # Hard auth errors do not retry
                    if resp.status_code in (401, 403):
                        raise httpx.HTTPStatusError(
                            f"LLM Authentication failed (HTTP {resp.status_code}): {resp.text[:100]}",
                            request=resp.request,
                            response=resp,
                        )

                    resp.raise_for_status()
                    data = resp.json()

                    # Extract text content (supports OpenAI choices envelope or direct JSON)
                    if "choices" in data and data["choices"]:
                        choice = data["choices"][0]
                        content = _normalize_message_content(choice.get("message", {}).get("content", ""))
                    elif "content" in data and isinstance(data["content"], (str, list)):
                        content = _normalize_message_content(data["content"])
                    else:
                        content = json.dumps(data, ensure_ascii=False)

                    # Extract usage
                    usage = data.get("usage", {}) if isinstance(data, dict) else {}
                    p_tok = usage.get("prompt_tokens", 0)
                    c_tok = usage.get("completion_tokens", 0)
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
                    # Do not retry on 400, 401, 403, 404
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401, 403, 404):
                        raise

                    if attempts <= self.config.max_retries:
                        delay = backoff_delays[min(attempts - 1, len(backoff_delays) - 1)]
                        logger.warning("LLM request error (%s). Retrying in %ss...", exc, delay)
                        await asyncio.sleep(delay)
                    else:
                        break

        raise last_error or RuntimeError("LLM request failed after retries.")

    def chat_completion_sync(
        self,
        task_type: str,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        use_json_mode: bool = True,
        use_cache: bool = True,
    ) -> Union[Dict[str, Any], List[Any]]:
        """Synchronously call chat completion with caching and backoff."""
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

        url = self.normalize_url(self.config.base_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key.strip()}",
        }

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        backoff_delays = [0.05, 0.1, 0.2] if self.transport else [1.0, 2.0, 4.0]
        attempts = 0
        last_error: Optional[Exception] = None

        req_timeout = httpx.Timeout(self.config.timeout_sec, connect=min(float(self.config.timeout_sec), 10.0))
        with httpx.Client(transport=self.transport, timeout=req_timeout) as client:
            while attempts <= self.config.max_retries:
                attempts += 1
                try:
                    self.total_requests += 1
                    resp = client.post(url, headers=headers, json=payload)

                    # Handle models rejecting response_format (400 error)
                    if resp.status_code == 400 and use_json_mode:
                        logger.debug("Server rejected JSON mode (HTTP 400); retrying without response_format")
                        payload.pop("response_format", None)
                        use_json_mode = False
                        continue

                    # Hard auth errors do not retry
                    if resp.status_code in (401, 403):
                        raise httpx.HTTPStatusError(
                            f"LLM Authentication failed (HTTP {resp.status_code}): {resp.text[:100]}",
                            request=resp.request,
                            response=resp,
                        )

                    resp.raise_for_status()
                    data = resp.json()

                    # Extract text content (supports OpenAI choices envelope or direct JSON)
                    if "choices" in data and data["choices"]:
                        choice = data["choices"][0]
                        content = _normalize_message_content(choice.get("message", {}).get("content", ""))
                    elif "content" in data and isinstance(data["content"], (str, list)):
                        content = _normalize_message_content(data["content"])
                    else:
                        content = json.dumps(data, ensure_ascii=False)

                    # Extract usage
                    usage = data.get("usage", {}) if isinstance(data, dict) else {}
                    p_tok = usage.get("prompt_tokens", 0)
                    c_tok = usage.get("completion_tokens", 0)
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
                    # Do not retry on 400, 401, 403, 404
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (400, 401, 403, 404):
                        raise

                    if attempts <= self.config.max_retries:
                        delay = backoff_delays[min(attempts - 1, len(backoff_delays) - 1)]
                        logger.warning("LLM request error (%s). Retrying in %ss...", exc, delay)
                        time.sleep(delay)
                    else:
                        break

        raise last_error or RuntimeError("LLM request failed after retries.")

    def test_connection(self) -> Dict[str, Any]:
        """Test API connectivity and return latency and model info."""
        if not self.config.base_url:
            return {"ok": False, "error": "Base URL not set"}

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
        try:
            with httpx.Client(transport=self.transport, timeout=10.0) as client:
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

        url = self.normalize_models_url(self.config.base_url)
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"

        timeout = min(max(float(self.config.timeout_sec or 10.0), 5.0), 15.0)
        try:
            with httpx.Client(transport=self.transport, timeout=timeout) as client:
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
