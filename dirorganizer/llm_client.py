from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class LocalLLMClient:
    base_url: str
    model: str
    api_key: str
    api_mode: str = "chat_completions"
    extra_body: dict[str, object] = field(default_factory=dict)
    timeout: int = 120
    temperature: float = 0.0
    max_output_tokens: int = 4096
    request_count: int = 0
    request_seconds: float = 0.0

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, object]:
        attempts = self._build_payload_attempts(system_prompt=system_prompt, user_prompt=user_prompt)
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

    def host(self) -> str | None:
        parsed = urlparse(self.base_url)
        return parsed.hostname

    def _build_payload_attempts(self, *, system_prompt: str, user_prompt: str) -> list[dict[str, object]]:
        if self.api_mode == "responses":
            base_payload = {
                "model": self.model,
                "instructions": f"{system_prompt}\n\nReturn JSON only.",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": user_prompt,
                            }
                        ],
                    }
                ],
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
            }
            return [
                _merge_dicts(
                    {
                        **base_payload,
                        "text": {"format": {"type": "json_object"}},
                    },
                    self.extra_body,
                ),
                _merge_dicts(base_payload, self.extra_body),
            ]

        base_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        return [
            _merge_dicts(
                {
                    **base_payload,
                    "response_format": {"type": "json_object"},
                },
                self.extra_body,
            ),
            _merge_dicts(base_payload, self.extra_body),
        ]

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        endpoint = self._build_endpoint()
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        self.request_count += 1
        started_at = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"failed to reach local LLM endpoint: {exc}") from exc
        finally:
            self.request_seconds += time.perf_counter() - started_at

        data = json.loads(raw)
        content = self._extract_content(data)
        return _extract_json_object(content)

    def _build_endpoint(self) -> str:
        trimmed = self.base_url.rstrip("/")
        if trimmed.endswith("/chat/completions") or trimmed.endswith("/responses"):
            return trimmed
        suffix = "/responses" if self.api_mode == "responses" else "/chat/completions"
        return f"{trimmed}{suffix}"

    def _extract_content(self, data: dict[str, object]) -> str:
        content = _extract_responses_output_text(data)
        if content is None:
            content = _extract_chat_completions_text(data)
        if content is None:
            raise RuntimeError("LLM response content was not text")
        return content


def _strip_thinking_tags(text: str) -> str:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned)
    return cleaned.strip()


def _extract_json_object(text: str) -> dict[str, object]:
    cleaned = _strip_thinking_tags(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"LLM response did not contain JSON: {text[:200]}")
    snippet = cleaned[start : end + 1]
    return json.loads(snippet)


def _is_retryable_payload_error(error: RuntimeError) -> bool:
    message = str(error).lower()
    markers = (
        "response_format",
        "json_object",
        "text.format",
        "the model has crashed",
        '"code":400',
        "http 400",
    )
    return any(marker in message for marker in markers)


def _extract_responses_output_text(data: dict[str, object]) -> str | None:
    output = data.get("output")
    if not isinstance(output, list):
        return None
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text")
            if isinstance(text, str):
                parts.append(text)
    merged = "".join(parts)
    return merged or None


def _extract_chat_completions_text(data: dict[str, object]) -> str | None:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    merged = "".join(parts)
    return merged or None


def _merge_dicts(base: dict[str, object], extra: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(current, value)
            continue
        merged[key] = value
    return merged
