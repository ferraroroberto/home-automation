"""Thin HTTP client of the local-llm-hub for the benchmark.

Two surfaces:
- Admin model lifecycle (``/admin/api/models[...]/start|stop``, readiness) so
  the runner can swap models within the GPU's VRAM budget.
- Chat: ``free`` mode through the hub's OpenAI-shape ``/v1/chat/completions``
  (no grammar — measures raw schema-validity), and ``grammar`` mode straight
  to the llama-server backend port (the hub drops ``response_format`` on its
  public route — see issue notes), where ``response_format: json_schema``
  forces 100%-valid JSON.

All calls are loopback, so the hub's bearer token (a non-secret dummy) is not
needed. Latency is measured with ``time.monotonic_ns()``; streaming is used
for local models to capture time-to-first-token (TTFT).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class ChatResult:
    content: str
    ttft_ms: Optional[float]   # None when not streamed (cloud non-stream path)
    total_ms: float
    ok: bool
    error: str = ""


class HubClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8000, timeout: float = 120.0):
        self.base = f"http://{host}:{port}"
        self.timeout = timeout
        self._c = httpx.Client(timeout=timeout)

    # --- admin: model lifecycle -------------------------------------------

    def list_models(self) -> List[Dict[str, Any]]:
        r = self._c.get(f"{self.base}/admin/api/models")
        r.raise_for_status()
        return r.json().get("models", [])

    def model_row(self, model_id: str) -> Optional[Dict[str, Any]]:
        for m in self.list_models():
            if m.get("id") == model_id or model_id in (m.get("aliases") or []):
                return m
        return None

    def start(self, model_id: str) -> str:
        r = self._c.post(f"{self.base}/admin/api/models/{model_id}/start")
        # 409 = already running, which is fine for our purposes.
        if r.status_code not in (200, 409):
            r.raise_for_status()
        try:
            return r.json().get("detail", "")
        except Exception:  # noqa: BLE001
            return r.text[:200]

    def stop(self, model_id: str) -> str:
        r = self._c.post(f"{self.base}/admin/api/models/{model_id}/stop")
        if r.status_code not in (200, 409):
            r.raise_for_status()
        try:
            return r.json().get("detail", "")
        except Exception:  # noqa: BLE001
            return r.text[:200]

    def wait_ready(self, model_id: str, timeout_s: float = 240.0, poll_s: float = 2.0) -> bool:
        """Poll the admin model list until the backend reports reachable."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            row = self.model_row(model_id)
            if row and row.get("reachable"):
                return True
            time.sleep(poll_s)
        return False

    # --- chat -------------------------------------------------------------

    def chat_free_stream(
        self, model: str, messages: List[Dict[str, Any]], *, max_tokens: int = 128,
        temperature: float = 0.0,
    ) -> ChatResult:
        """Streamed chat through the hub (TTFT + total). For local models."""
        url = f"{self.base}/v1/chat/completions"
        payload = {
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": True,
        }
        return self._stream(url, payload)

    def chat_free_blocking(
        self, model: str, messages: List[Dict[str, Any]], *, max_tokens: int = 128,
        temperature: float = 0.0,
    ) -> ChatResult:
        """Non-streamed chat through the hub. For the cloud (claude) path."""
        url = f"{self.base}/v1/chat/completions"
        payload = {
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature,
        }
        t0 = time.monotonic_ns()
        try:
            r = self._c.post(url, json=payload)
            total = (time.monotonic_ns() - t0) / 1e6
            if r.status_code >= 400:
                return ChatResult("", None, total, False, f"HTTP {r.status_code}: {r.text[:200]}")
            content = (r.json()["choices"][0]["message"].get("content") or "")
            return ChatResult(content, None, total, True)
        except Exception as e:  # noqa: BLE001
            return ChatResult("", None, (time.monotonic_ns() - t0) / 1e6, False, str(e))

    def chat_backend_stream(
        self, backend_base: str, model: str, messages: List[Dict[str, Any]], *,
        max_tokens: int = 128, temperature: float = 0.0,
        response_format: Optional[Dict[str, Any]] = None,
        chat_template_kwargs: Optional[Dict[str, Any]] = None,
    ) -> ChatResult:
        """Streamed chat straight to the llama-server backend port.

        The hub's public route drops ``response_format`` / ``chat_template_kwargs``
        (it only forwards ``tools``/``tool_choice``), so the no-think and
        grammar-constrained modes must address the backend directly.
        """
        url = f"{backend_base.rstrip('/')}/chat/completions"
        payload: Dict[str, Any] = {
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": True,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if chat_template_kwargs is not None:
            payload["chat_template_kwargs"] = chat_template_kwargs
        return self._stream(url, payload)

    # --- internals --------------------------------------------------------

    def _stream(self, url: str, payload: Dict[str, Any]) -> ChatResult:
        t0 = time.monotonic_ns()
        ttft: Optional[float] = None
        parts: List[str] = []
        try:
            with self._c.stream("POST", url, json=payload,
                                 headers={"Accept": "text/event-stream"}) as r:
                if r.status_code >= 400:
                    body = r.read().decode("utf-8", "replace")
                    return ChatResult("", None, (time.monotonic_ns() - t0) / 1e6, False,
                                      f"HTTP {r.status_code}: {body[:200]}")
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        if ttft is None:
                            ttft = (time.monotonic_ns() - t0) / 1e6
                        parts.append(piece)
            total = (time.monotonic_ns() - t0) / 1e6
            return ChatResult("".join(parts), ttft, total, True)
        except Exception as e:  # noqa: BLE001
            return ChatResult("".join(parts), ttft, (time.monotonic_ns() - t0) / 1e6, False, str(e))

    def close(self) -> None:
        self._c.close()
