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
    body_key: str = "face_arms"
    target_iu: int = 1000
    application: str = "typical"  # how thickly sunscreen goes on (uv_dose.exposure.APPLICATION_CHOICES)
    source: str = "best"
    dark: bool = False
    # The day's plan, as times of day: [{"start": "10:00", "end": "14:00", "shade": "sun",
    # "surface": "ground", "spf": 1}, ...]. Validated by the window that uses it.
    outings: list[dict] = field(default_factory=list)
    quiz_answers: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or config_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        if "outings" not in raw and "start" in raw:  # files from before outings: one window + one SPF
            raw["outings"] = [{"start": raw.get("start"), "end": raw.get("end"), "spf": raw.get("spf", 1)}]
        if not isinstance(raw.get("outings", []), list):
            raw.pop("outings")
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
