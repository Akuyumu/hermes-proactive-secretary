"""Fail-closed native enable/disable gate shared by the scheduled runners.

The native scheduler executes the installed runner scripts on their cron
cadence regardless of whether the plugin is enabled. This gate lets BOTH
runners stop before any work when the native toggle says the plugin is off.

Semantics (deliberately conservative; this is a standalone opt-in plugin):

* ``plugins.disabled`` wins: if this plugin is denied there, the gate is off.
* ``plugins.enabled`` must explicitly list this plugin. An absent, malformed or
  empty enabled list fails CLOSED (off).
* A missing/removed plugin root fails closed.
* An unreadable config fails closed.

Only ``plugins.enabled`` and ``plugins.disabled`` are read; no other config
field is used or printed. The config is the profile's ``config.yaml`` (or
``config.json``), falling back to the supported Hermes loader when neither file
exists. This module never imports the pipeline and never reads runtime state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from proactive_pilot import ownerpaths as _ownerpaths  # noqa: E402

PLUGIN_ID = "hermes-proactive-portal"


def _hermes_home() -> Path:
    return _ownerpaths.hermes_home()


def _read_yaml(path: Path) -> Any:
    try:
        import yaml  # provided by the Hermes runtime
    except Exception:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_plugin_config(hermes_home: Path | str | None = None) -> dict[str, Any] | None:
    """Return the active profile's config mapping, or ``None`` when unreadable.

    The profile ``config.yaml`` / ``config.json`` is preferred so the check is
    deterministic and isolated per ``HERMES_HOME``; the supported Hermes loader
    is used only when no file exists at that profile. When a caller pins an
    explicit ``hermes_home``, that profile's files are the only source (the
    loader is not consulted), so tests can never reach a live profile.
    """
    explicit = hermes_home is not None
    home = Path(hermes_home) if explicit else _hermes_home()
    yaml_file = home / "config.yaml"
    if yaml_file.is_file():
        data = _read_yaml(yaml_file)
        return data if isinstance(data, dict) else None
    json_file = home / "config.json"
    if json_file.is_file():
        data = _read_json(json_file)
        return data if isinstance(data, dict) else None
    if explicit:
        return None
    try:
        from hermes_cli.config import load_config_readonly
        data = load_config_readonly()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _names(value: Any) -> set[str] | None:
    """Return non-empty string names, or ``None`` when the value is malformed."""
    if value is None:
        return set()
    if not isinstance(value, (list, tuple, set)):
        return None
    out: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return None
        name = item.strip()
        if name:
            out.add(name)
    return out


def plugin_enabled(
    plugin_root: Path | str | None = None,
    *,
    hermes_home: Path | str | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """True only when the native config explicitly enables this plugin.

    ``config`` may be supplied directly (tests); otherwise it is loaded for the
    active profile. Every ambiguous or unreadable case returns False.
    """
    root = Path(plugin_root) if plugin_root is not None else Path(__file__).resolve().parent
    if not root.is_dir():
        return False
    if config is None:
        try:
            config = load_plugin_config(hermes_home)
        except _ownerpaths.ProfileResolutionError:
            return False
    if not isinstance(config, dict):
        return False
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return False
    disabled = _names(plugins.get("disabled"))
    if disabled is None:
        return False
    if PLUGIN_ID in disabled:
        return False
    enabled = _names(plugins.get("enabled"))
    if not enabled:
        return False
    return PLUGIN_ID in enabled
