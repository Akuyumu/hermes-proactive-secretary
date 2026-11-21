"""One-time copy of the retired plugin id's owner-only data root.

The plugin id changed from ``hermes-proactive-secretary`` to
``hermes-proactive-portal``. This module copies an existing legacy tree into the
new root the first time the new root is absent or empty. It is deliberately
small, stdlib-only and fail-closed:

* the legacy tree is only ever read; it is never modified or deleted, so it
  remains a rollback copy;
* files are copied into a temporary sibling first, made owner-only
  (``0700`` directories, ``0600`` files), then moved into place with
  ``os.replace`` so a partial copy can never be mistaken for migrated state;
* a second call is a no-op once the new root has content;
* every failure is a diagnostic, never an exception into plugin load. The
  plugin still starts with an empty new root rather than a half-copied one.

The caller owns logging. :func:`migrate_legacy_data_root` returns a bounded
result; it never includes per-file paths or file contents.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import ownerpaths

# Historical identity only: this is the source root of the one-time migration.
LEGACY_PLUGIN_NAME = "hermes-proactive-secretary"


class MigrationError(RuntimeError):
    """The legacy tree could not be copied safely."""


def legacy_data_root() -> Path:
    """The retired id's root inside the active profile, never an override."""
    return ownerpaths.hermes_home() / "plugin-data" / LEGACY_PLUGIN_NAME


def _is_empty(root: Path) -> bool:
    try:
        return not any(root.iterdir())
    except OSError as exc:
        raise MigrationError(
            f"cannot inspect the new data root ({type(exc).__name__})") from exc


def _walk_legacy(source: Path) -> list[Path]:
    """Return regular files and directories, refusing symlinks and specials."""
    entries: list[Path] = []
    for current, dirnames, filenames in os.walk(source, followlinks=False):
        base = Path(current)
        for name in sorted(dirnames):
            path = base / name
            if path.is_symlink():
                raise MigrationError("legacy data tree contains a symlink")
            if not path.is_dir():
                raise MigrationError("legacy data tree contains a non-directory")
            entries.append(path)
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink():
                raise MigrationError("legacy data tree contains a symlink")
            if not path.is_file():
                raise MigrationError("legacy data tree contains a non-regular file")
            entries.append(path)
    return entries


def _copy_tree(source: Path, target: Path) -> int:
    """Copy ``source`` into the existing empty ``target``; return file count."""
    files = 0
    for path in _walk_legacy(source):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            os.chmod(destination, 0o700)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(path, destination, follow_symlinks=False)
        except OSError as exc:
            raise MigrationError(
                f"cannot copy one legacy file ({type(exc).__name__})") from exc
        os.chmod(destination, 0o600)
        files += 1
    return files


def migrate_legacy_data_root(
    new_root: str | Path | None = None,
    legacy_root: str | Path | None = None,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Copy a legacy root into the new root once, idempotently and safely.

    ``new_root`` defaults to :func:`ownerpaths.data_root` and ``legacy_root`` to
    :func:`legacy_data_root`. Nothing is copied when the new root already has
    any content. No exception escapes; a failure is reported in ``error`` and
    the new root is left absent/empty.
    """
    result: dict[str, Any] = {
        "ok": True, "migrated": False, "files": 0,
        "new_root": "", "legacy_root": "", "error": "",
    }
    try:
        target = Path(new_root) if new_root is not None else ownerpaths.data_root()
        source = Path(legacy_root) if legacy_root is not None else legacy_data_root()
        result["new_root"] = str(target)
        result["legacy_root"] = str(source)
        if target.exists():
            if not target.is_dir():
                result["error"] = "new data root is not a directory"
                return result
            if not _is_empty(target):
                return result
        if not source.is_dir():
            return result
        if target.resolve(strict=False) == source.resolve(strict=False):
            result["error"] = "new and legacy data roots are the same"
            return result
        entries = _walk_legacy(source)
        if not entries:
            return result

        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        temp = Path(tempfile.mkdtemp(prefix=".portal-migration-", dir=target.parent))
        try:
            os.chmod(temp, 0o700)
            files = _copy_tree(source, temp)
            os.replace(temp, target)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
    except Exception as exc:  # noqa: BLE001 - migration never breaks plugin load
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        if log is not None:
            log("proactive-portal: legacy data migration failed; starting with "
                "an empty new root")
        return result

    result["migrated"] = True
    result["files"] = files
    if log is not None:
        log(f"proactive-portal: migrated {files} file(s) from the legacy data root")
    return result


def migrate_for_load() -> dict[str, Any]:
    """Load-hook entry point: migrate once and log one bounded diagnostic line."""
    return migrate_legacy_data_root(log=lambda message: print(message, file=sys.stderr))


__all__ = [
    "LEGACY_PLUGIN_NAME", "MigrationError", "legacy_data_root",
    "migrate_for_load", "migrate_legacy_data_root",
]
