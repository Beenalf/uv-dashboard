"""Remembered user choices (city, skin type, etc.), stored as JSON in the user's config folder."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "uv-dose" / "settings.json"


@dataclass
class Settings:
    site_code: str = "mel"
    skin_type: int = 2
    med_sed: float | None = None  # None -> use the skin type's default
    spf: int = 1  # 1 == no sunscreen
    body_key: str = "face_arms"
    target_iu: int = 1000
    source: str = "best"
    dark: bool = False
    start: str = "10:00"
    end: str = "14:00"
    quiz_answers: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or config_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in raw.items() if k in known})
        except TypeError:
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass  # preferences are a convenience; never crash over them
