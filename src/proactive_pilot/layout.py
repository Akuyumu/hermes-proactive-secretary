"""Detect stale legacy layout artifacts left by a copy-over upgrade.

Two distinct hazards are reported here, never repaired:

* root-level ``portal_*.py`` modules that shadow the same module in the real
  ``portal/`` subpackage, so an upgrade can silently run stale flat code;
* legacy ``secretary/`` trees or root-level ``secretary_*.py`` modules left
  behind by a copy-over upgrade from 1.0.4 or earlier.

The caller must remove the stale artifacts and reinstall rather than copying
over an old tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SHADOW_PREFIX = "portal_"
LEGACY_PREFIX = "secretary_"
LEGACY_DIRNAME = "secretary"


def legacy_shadows(root: Any = None) -> list[str]:
    """Return sorted root-level ``portal_*.py`` names shadowing ``portal/``.

    A file is reported only when it sits at the package root, is a regular file,
    and the same basename also exists inside ``portal/``.  A tree without a
    ``portal/`` directory has no shadow problem.  Any ``OSError`` yields an
    empty list so diagnostics never crash.
    """
    try:
        base = Path(root)
        subpackage = base / "portal"
        if not subpackage.is_dir():
            return []
        shadows: list[str] = []
        for path in base.iterdir():
            name = path.name
            if not (name.startswith(SHADOW_PREFIX) and name.endswith(".py")):
                continue
            if path.is_dir():
                continue
            if (subpackage / name).is_file():
                shadows.append(path.relative_to(base).as_posix())
        return sorted(shadows)
    except (OSError, TypeError):  # a missing/unusable root is a diagnostic, never a crash
        return []


def legacy_artifacts(root: Any = None) -> list[str]:
    """Return sorted stale legacy ``secretary`` artifacts at the package root.

    Reports a leftover ``secretary/`` directory and any root-level
    ``secretary_*.py`` module.  These are never valid after the rename; they are
    what a copy-over upgrade from the old layout leaves behind.  Any ``OSError``
    yields an empty list so diagnostics never crash.
    """
    try:
        base = Path(root)
        artifacts: list[str] = []
        if (base / LEGACY_DIRNAME).is_dir():
            artifacts.append(LEGACY_DIRNAME + "/")
        for path in base.iterdir():
            name = path.name
            if path.is_dir():
                continue
            if name.startswith(LEGACY_PREFIX) and name.endswith(".py"):
                artifacts.append(path.relative_to(base).as_posix())
        return sorted(artifacts)
    except (OSError, TypeError):  # a missing/unusable root is a diagnostic, never a crash
        return []


def is_shadowed(root: Any = None) -> bool:
    """Return whether :func:`legacy_shadows` finds any shadowing root module."""
    return bool(legacy_shadows(root))


def has_legacy_artifacts(root: Any = None) -> bool:
    """Return whether :func:`legacy_artifacts` finds any stale legacy artifact."""
    return bool(legacy_artifacts(root))
