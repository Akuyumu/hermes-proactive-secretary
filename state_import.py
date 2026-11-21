"""Explicit, non-destructive legacy-state import for ``hermes secretary setup``.

The legacy contract is generic and fixed:

* ``<legacy>/var/``                       -> the plugin's durable state tree
* ``<legacy>/policy/policy.json``         -> ``paths.policy_path()``
* ``<legacy>/policy/preferences.json``    -> ``paths.preferences_path()``

Files are copied byte-for-byte with owner-only permissions (directories
``0700``, files ``0600``). Nothing is inferred or rebuilt, and the reminder
registry is copied verbatim. An import refuses any existing conflicting target
file; a matching digest is a safe idempotent no-op. There is no overwrite and no
force. Symlinks and path traversal are rejected up front. Only a narrow,
documented set of transient/cache/shadow artifacts is excluded, and excluded
filenames are reported without their contents. No cron job is read, created or
removed by an import.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = 1

# Narrow, documented exclusions only. Durable registries are never omitted by a
# speculative allowlist: everything under ``var/`` is copied unless it matches
# one of these clearly-transient forms.
EXCLUDED_NAMES = (".DS_Store",)
EXCLUDED_SUFFIXES = (".lock", ".tmp", ".temp", ".swp", ".swo", ".pyc")
EXCLUDED_DIR_NAMES = {
    "__pycache__": "cache",
    ".cache": "cache",
    "cache": "cache",
    "shadow": "shadow-artifact",
}


class LegacyTreeError(RuntimeError):
    """The legacy tree is missing, unsafe, or not a directory tree."""


class ImportConflictError(RuntimeError):
    """A target file already exists with different content."""

    def __init__(self, message: str, manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.manifest = manifest or {}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _private_dirs(path: Path, stop: Path) -> None:
    """Create ``path`` and make every component down to ``stop`` owner-only."""
    path.mkdir(parents=True, exist_ok=True)
    current = path
    while True:
        _chmod(current, 0o700)
        if current == stop or current.parent == current:
            break
        if stop not in current.parents:
            break
        current = current.parent


def _exclusion_reason(rel: PurePosixPath) -> str:
    if rel.name in EXCLUDED_NAMES:
        return "transient"
    if rel.suffix.lower() in EXCLUDED_SUFFIXES:
        return "transient"
    for part in rel.parts:
        reason = EXCLUDED_DIR_NAMES.get(part)
        if reason:
            return reason
    return ""


def _scan_legacy_files(legacy_root: Path) -> list[Path]:
    """Collect every regular file under ``legacy_root``; refuse any symlink."""
    if legacy_root.is_symlink():
        raise LegacyTreeError(f"legacy root is a symlink: {legacy_root}")
    if not legacy_root.is_dir():
        raise LegacyTreeError(f"legacy tree is not a directory: {legacy_root}")
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(legacy_root, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            candidate = base / name
            if candidate.is_symlink():
                raise LegacyTreeError(
                    f"refusing symlink in legacy tree: {candidate.relative_to(legacy_root)}")
        for name in filenames:
            candidate = base / name
            if candidate.is_symlink():
                raise LegacyTreeError(
                    f"refusing symlink in legacy tree: {candidate.relative_to(legacy_root)}")
            files.append(candidate)
    return files


def _map_target(rel: PurePosixPath, state_dir: Path, policy_path: Path,
                preferences_path: Path) -> Path | None:
    parts = rel.parts
    if parts and parts[0] == "var" and len(parts) > 1:
        target = state_dir.joinpath(*parts[1:])
    elif rel.as_posix() == "policy/policy.json":
        target = policy_path
    elif rel.as_posix() == "policy/preferences.json":
        target = preferences_path
    else:
        return None
    # Path-traversal guard: the resolved posix form must not climb out.
    if ".." in PurePosixPath(*parts).parts:
        return None
    return target


def _has_symlink_ancestor(target: Path, stop: Path) -> bool:
    """True when any existing component from ``stop`` down to ``target`` is a link."""
    try:
        current = target
        stop = stop.resolve() if stop.exists() else stop
        while True:
            if current.exists() and current.is_symlink():
                return True
            if current == stop or current.parent == current:
                break
            current = current.parent
    except OSError:
        return True
    return False


def _plan(legacy_root: Path, *, state_dir: Path, policy_path: Path,
          preferences_path: Path, target_root: Path) -> dict[str, Any]:
    files = _scan_legacy_files(legacy_root)
    entries: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for path in sorted(files):
        rel = PurePosixPath(path.relative_to(legacy_root).as_posix())
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LegacyTreeError(f"cannot read legacy file {rel}: {exc}") from exc
        digest = _sha256_bytes(data)
        entry: dict[str, Any] = {"rel": rel.as_posix(), "size": len(data), "sha256": digest}
        reason = _exclusion_reason(rel)
        if reason:
            entry["action"] = "excluded"
            entry["reason"] = reason
            entries.append(entry)
            continue
        target = _map_target(rel, state_dir, policy_path, preferences_path)
        if target is None:
            entry["action"] = "excluded"
            entry["reason"] = "outside-legacy-contract"
            entries.append(entry)
            continue
        entry["target"] = str(target)
        resolution = "copy"
        if _has_symlink_ancestor(target, target_root):
            resolution = "conflict"
            entry["reason"] = "target-path-contains-symlink"
        elif target.exists():
            if not target.is_file():
                resolution = "conflict"
                entry["reason"] = "target-exists-not-a-file"
            else:
                try:
                    same = _sha256_file(target) == digest
                except OSError:
                    same = False
                if same:
                    resolution = "identical"
                else:
                    resolution = "conflict"
                    entry["reason"] = "target-differs-from-legacy"
        entry["action"] = resolution
        if resolution == "conflict":
            conflicts.append({"rel": entry["rel"], "target": str(target),
                              "reason": entry.get("reason", "")})
        entries.append(entry)
    digest_input = json.dumps(
        sorted((e["rel"], e["sha256"]) for e in entries),
        sort_keys=True, separators=(",", ":"))
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "legacy_root": str(legacy_root),
        "target_root": str(target_root),
        "generated_at": _now_iso(),
        "dry_run": True,
        "files": entries,
        "excluded": sorted(e["rel"] for e in entries if e["action"] == "excluded"),
        "conflicts": conflicts,
        "counts": {
            "total": len(entries),
            "copied": sum(1 for e in entries if e["action"] == "copy"),
            "identical": sum(1 for e in entries if e["action"] == "identical"),
            "excluded": sum(1 for e in entries if e["action"] == "excluded"),
            "conflicts": len(conflicts),
        },
        "manifest_sha256": _sha256_bytes(digest_input.encode("utf-8")),
        "ok": not conflicts,
    }
    return manifest


def _default_targets() -> tuple[Path, Path, Path, Path, Path]:
    if __package__:
        from . import paths
    else:
        import paths  # standalone test/tool invocation from the plugin root
    return (paths.state_dir(), paths.policy_path(), paths.preferences_path(),
            paths.data_root(), paths.migration_manifest_path())


def preview(legacy_root: Path | str, *, state_dir: Path | None = None,
            policy_path: Path | None = None, preferences_path: Path | None = None,
            target_root: Path | None = None) -> dict[str, Any]:
    """Return the machine-readable inventory without touching the target."""
    d_state, d_policy, d_prefs, d_root, _manifest = _default_targets()
    manifest = _plan(Path(legacy_root),
                     state_dir=state_dir or d_state,
                     policy_path=policy_path or d_policy,
                     preferences_path=preferences_path or d_prefs,
                     target_root=target_root or d_root)
    manifest["dry_run"] = True
    return manifest


def import_legacy(legacy_root: Path | str, *, state_dir: Path | None = None,
                  policy_path: Path | None = None, preferences_path: Path | None = None,
                  target_root: Path | None = None) -> dict[str, Any]:
    """Copy the legacy durable state; refuse any conflicting target file.

    Raises :class:`ImportConflictError` (carrying the manifest) when the target
    already holds different content. A second identical run is a no-op.
    """
    d_state, d_policy, d_prefs, d_root, manifest_path = _default_targets()
    state_dir = state_dir or d_state
    policy_path = policy_path or d_policy
    preferences_path = preferences_path or d_prefs
    target_root = target_root or d_root
    manifest = _plan(Path(legacy_root), state_dir=state_dir, policy_path=policy_path,
                     preferences_path=preferences_path, target_root=target_root)
    manifest["dry_run"] = False
    if manifest["conflicts"]:
        raise ImportConflictError(
            "refusing to import: target already holds different durable state "
            f"({len(manifest['conflicts'])} conflict(s))", manifest)

    os.makedirs(target_root, mode=0o700, exist_ok=True)
    _chmod(target_root, 0o700)
    for entry in manifest["files"]:
        if entry["action"] != "copy":
            continue
        source = Path(legacy_root) / entry["rel"]
        target = Path(entry["target"])
        _private_dirs(target.parent, target_root)
        data = source.read_bytes()
        tmp = target.with_name(target.name + ".import-tmp")
        tmp.write_bytes(data)
        _chmod(tmp, 0o600)
        os.replace(tmp, target)
        _chmod(target, 0o600)
    _private_dirs(state_dir, target_root)
    _private_dirs(policy_path.parent, target_root)
    manifest["manifest_path"] = str(manifest_path)
    os.makedirs(manifest_path.parent, mode=0o700, exist_ok=True)
    _chmod(manifest_path.parent, 0o700)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    _chmod(manifest_path, 0o600)
    return manifest
