"""Remember the window's folders between runs."""

import json
from pathlib import Path

SETTINGS_JSON = Path(__file__).resolve().parent / "gui_settings.json"


def load_settings(path=SETTINGS_JSON):
    """Return the saved settings, or nothing if the file is missing or unreadable."""
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def save_settings(settings, path=SETTINGS_JSON):
    """Save the settings. Losing them is harmless, so a failed write is ignored."""
    try:
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
