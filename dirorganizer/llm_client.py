from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class LocalLLMClient:
    base_url: str
    model: str
    api_key: str
    timeout: int = 120
    temperature: float = 0.0
    max_output_tokens: int = 1200

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        base_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        attempts = [
            {
                **base_payload,
                "response_format": {"type": "json_object"},
            },
            base_payload,
        ]
        last_error: RuntimeError | None = None
        for payload in attempts:
            try:
                return self._request(payload)
            except RuntimeError as error:
                last_error = error
                if not _is_retryable_payload_error(error):
                    raise
        assert last_error is not None
        raise last_error

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"failed to reach local LLM endpoint: {exc}") from exc

        data = json.loads(raw)
        choices = data.get("choices")
        if not choices:
            raise RuntimeError("LLM response did not contain choices")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                text = item.get("text")
                if text:
                    parts.append(text)
            content = "".join(parts)
        if not isinstance(content, str):
            raise RuntimeError("LLM response content was not text")
        return _extract_json_object(content)


def _extract_json_object(text: str) -> dict[str, object]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"LLM response did not contain JSON: {text[:200]}")
    snippet = text[start : end + 1]
    return json.loads(snippet)


def _is_retryable_payload_error(error: RuntimeError) -> bool:
    message = str(error).lower()
    markers = (
        "response_format",
        "json_object",
        "the model has crashed",
        '"code":400',
        "http 400",
    )
    return any(marker in message for marker in markers)
