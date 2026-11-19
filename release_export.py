"""Compatibility shim for the relocated repository release exporter.

New development and maintainer automation should use
``scripts/release_export.py``. This root filename remains importable for existing
commands and tests, re-exports the implementation (including underscore helpers),
and keeps the same mutable module identity.
"""
from __future__ import annotations

import sys

from scripts import release_export as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
