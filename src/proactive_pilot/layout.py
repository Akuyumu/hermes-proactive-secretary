"""Detect legacy root-level ``secretary_*.py`` modules that shadow ``secretary/``.

When an upgrade copies over an existing flat plugin directory, stale
root-level ``secretary_*.py`` modules can be imported instead of the real
modules in the ``secretary/`` subpackage.  The plugin then silently runs stale
code.  This hazard is detected here, not repaired: the caller must remove the
stale root modules and reinstall rather than copying over an old tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SHADOW_PREFIX = "secretary_"


def legacy_shadows(root: Any = None) -> list[str]:
    """Return sorted root-level ``secretary_*.py`` names shadowing ``secretary/``.

    A file is reported only when it sits at the package root, is a regular file,
    and the same basename also exists inside ``secretary/``.  A tree without a
    ``secretary/`` directory is pre-1.0.3 and is not a shadow problem.  Any
    ``OSError`` yields an empty list so diagnostics never crash.
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


def is_shadowed(root: Any = None) -> bool:
    """Return whether :func:`legacy_shadows` finds any shadowing root module."""
    return bool(legacy_shadows(root))
