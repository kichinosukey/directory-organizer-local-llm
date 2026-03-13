from __future__ import annotations

import json
from pathlib import Path

from dirorganizer.presets import DEFAULT_PRESET_NAME, get_preset


def load_rules(path: str | None, *, preset_name: str = DEFAULT_PRESET_NAME) -> dict[str, object]:
    merged = get_preset(preset_name).copy_rules()
    if path is None:
        return merged

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    for key, value in payload.items():
        merged[key] = value
    return merged
