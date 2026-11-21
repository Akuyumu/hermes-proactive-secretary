"""Detect stale legacy layout artifacts left by a copy-over upgrade.

Two distinct hazards are reported here, never repaired:

* root-level ``secretary_*.py`` modules that shadow the same module in the
  real ``secretary/`` subpackage, so an upgrade can silently run stale flat
  code;
* legacy ``portal/`` trees or root-level ``portal_*.py`` modules left behind by
  a copy-over upgrade from the previous portal identity.

The caller must remove the stale artifacts and reinstall rather than copying
over an old tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SHADOW_PREFIX = "secretary_"
LEGACY_PREFIX = "portal_"
LEGACY_DIRNAME = "portal"


def legacy_shadows(root: Any = None) -> list[str]:
    """Return sorted root-level ``secretary_*.py`` names shadowing ``secretary/``.

    A file is reported only when it sits at the package root, is a regular file,
    and the same basename also exists inside ``secretary/``.  A tree without a
    ``secretary/`` directory has no shadow problem.  Any ``OSError`` yields an
    empty list so diagnostics never crash.
    """
    try:
        base = Path(root)
        subpackage = base / "secretary"
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
    """Return sorted stale legacy ``portal`` artifacts at the package root.

    Reports a leftover ``portal/`` directory and any root-level
    ``portal_*.py`` module.  These are never valid after the rename; they are
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
