"""Shared, stdlib-only profile-owner path resolution.

Every owner surface (root path helpers, preparation artifacts, scheduler gate,
collector/source locations and token locations) resolves the active profile
through this one small helper. It uses only the host's public profile API:

* ``hermes_constants.get_hermes_home`` for the context-local active home;
* ``hermes_constants.get_routing_process_hermes_home`` only to decide whether
  the process's data override belongs to its own launch profile.

A genuinely absent host module keeps the documented standalone
``HERMES_HOME``/``~/.hermes`` fallback. A present module whose getter or launch
scope cannot be resolved raises :class:`ProfileResolutionError`; it never falls
back to the launch profile or ambient environment.
"""
from __future__ import annotations

import os
from pathlib import Path

PLUGIN_NAME = "hermes-proactive-secretary"
DATA_ENV = "HERMES_PROACTIVE_SECRETARY_DATA"
_NATIVE_MODULE = "hermes_constants"


class ProfileResolutionError(RuntimeError):
    """The host's active profile scope could not be resolved safely."""


def _native_constants():
    """Return the host module, or ``None`` only when it is genuinely absent."""
    try:
        import hermes_constants
    except ImportError as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == _NATIVE_MODULE:
            return None
        raise ProfileResolutionError(
            "cannot load the host profile-scope module") from exc
    except Exception as exc:  # noqa: BLE001 - never fall back to launch state
        raise ProfileResolutionError(
            "cannot load the host profile-scope module") from exc
    return hermes_constants


def env_home() -> Path:
    """Standalone home from ``$HERMES_HOME`` or ``~/.hermes``."""
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override)
    return Path.home() / ".hermes"


def _native_home(native, name: str, description: str) -> Path:
    try:
        getter = getattr(native, name)
        return Path(getter())
    except Exception as exc:  # noqa: BLE001 - fail closed, never read launch A
        raise ProfileResolutionError(
            f"cannot resolve the {description} Hermes profile") from exc


def hermes_home() -> Path:
    """Active Hermes home from the host's context-local profile scope."""
    native = _native_constants()
    if native is None:
        return env_home()
    return _native_home(native, "get_hermes_home", "active")


def launch_home() -> Path:
    """The process's own launch home (pinned host home, else process env home)."""
    native = _native_constants()
    if native is None:
        return env_home()
    return _native_home(
        native, "get_routing_process_hermes_home", "launch")


def _same_path(left: Path, right: Path) -> bool:
    def key(value: Path) -> str:
        try:
            return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))
        except (OSError, RuntimeError):
            return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))

    return key(left) == key(right)


def data_root() -> Path:
    """Owner-only plugin data root for the active profile.

    ``HERMES_PROACTIVE_SECRETARY_DATA`` belongs to the process's own launch
    profile (and standalone CLI). A routed foreign profile ignores it and uses
    its own profile's ``plugin-data`` directory instead.
    """
    native = _native_constants()
    override = os.environ.get(DATA_ENV)
    if native is None:
        if override:
            return Path(override)
        return env_home() / "plugin-data" / PLUGIN_NAME
    active = _native_home(native, "get_hermes_home", "active")
    if override:
        launch = _native_home(
            native, "get_routing_process_hermes_home", "launch")
        if _same_path(active, launch):
            return Path(override)
    return active / "plugin-data" / PLUGIN_NAME


def owner_path(relative: str | Path, explicit: str | Path | None = None) -> Path:
    """An owner location under the active profile unless explicitly configured."""
    if explicit is not None:
        return Path(explicit)
    return hermes_home() / Path(relative)


__all__ = [
    "DATA_ENV", "PLUGIN_NAME", "ProfileResolutionError", "data_root", "env_home",
    "hermes_home", "launch_home", "owner_path",
]
