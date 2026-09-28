"""
Static configuration loader for Pishak Home.

Only truly infrastructure-level settings live here: where the process
binds, the log level, and the database path. Everything a person would
reasonably want to change from the dashboard (camera, motion, recording,
telegram, security) is GUI-editable and lives in the database — see
app/settings_manager.py.

config/settings.yaml (gitignored, local to each machine) is only consulted
as a one-time *seed* for those GUI-editable sections, the very first time
the app boots with a fresh database. After that, the database is the
single source of truth and editing the YAML file again has no effect —
use the Settings page instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "settings.yaml"
EXAMPLE_CONFIG_PATH = BASE_DIR / "config" / "settings.example.yaml"

DEFAULTS: dict[str, Any] = {
    "app": {"host": "0.0.0.0", "port": 8000, "log_level": "info"},
    "database": {"path": "data/pishak.db"},
    "paths": {
        "recordings_dir": "recordings",
        "snapshots_dir": "snapshots",
    },
}

# Sections that, if present in the YAML file, are used only to seed the
# database-backed SettingsManager on first boot (see app/main.py).
SEED_SECTIONS = ("camera", "motion", "recording", "storage", "telegram", "security")


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> dict[str, Any]:
    raw = _load_yaml()
    static_overrides = {k: v for k, v in raw.items() if k in DEFAULTS}
    return _deep_merge(DEFAULTS, static_overrides)


def load_seed_sections() -> dict[str, dict[str, Any]]:
    """Returns whatever GUI-editable sections exist in the YAML file, to be
    passed as SettingsManager's `seed` on first boot only."""
    raw = _load_yaml()
    return {k: v for k, v in raw.items() if k in SEED_SECTIONS and isinstance(v, dict)}


settings = load_config()
