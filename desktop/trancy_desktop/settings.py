from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TEMP = DATA / "temporary"
CACHE = DATA / "cache"

DEFAULTS = {
    "service_root": r"D:\dev\SubtitleService",
    "model": "small",
    "device": "auto",
    "translation_mode": "google",
    "overlay_width": 940,
    "primary_size": 30,
    "secondary_size": 24,
    "primary_color": "#FFFFFF",
    "secondary_color": "#E1D5FF",
    "caption_order": "english_first",
    "word_follow": True,
    "unread_brightness": 55,
    "opacity": 76,
    "overlay_x": None,
    "overlay_y": None,
    "overlay_locked": False,
    "follow_potplayer": True,
    "follow_browser": False,
    "dubbing_voice": "af_heart",
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA / "settings.json"
        self.values = dict(DEFAULTS)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.values.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, **values):
        self.values.update({k: v for k, v in values.items() if k in DEFAULTS})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
