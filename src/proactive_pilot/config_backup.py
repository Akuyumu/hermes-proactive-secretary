"""Owner-only, hash-verified backups for this plugin's own user settings.

A projection or migration can change what the native settings form shows. Before
that happens, this module snapshots *exactly* the records this plugin owns:

* the versioned ``user_config.json`` file (raw, validated later by the caller);
* the plugin's own ``policy.json`` copy and ``preferences.json``;
* the ordinary/native plugin settings values this plugin declares; and
* the plugin-scoped onboarding state record.

It never reads ``.env``, credential stores, host memories, unrelated plugin
directories or the rest of Hermes configuration. Files are written ``0600``
inside an owner-only ``0700`` backup directory and each entry is listed in
``manifest.json`` with its SHA-256 and byte length. A caller must create the
backup *before* writing; a failure here therefore prevents the write.

The public API is deliberately small: :func:`create_backup`,
:func:`verify_backup`, :func:`list_backups` and :func:`restore_backup`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = 1
BACKUP_DIRNAME = "backups"
ONBOARDING_STATE_KEY = "onboarding_state"
_FILE_RECORDS = ("user_config.json", "policy.json", "preferences.json")
_NATIVE_SETTINGS_FILE = "native_settings.json"
_ONBOARDING_FILE = "onboarding_state.json"
_ALLOWED_FILES = frozenset({
    *_FILE_RECORDS, _NATIVE_SETTINGS_FILE, _ONBOARDING_FILE,
})
_UNSET = object()


class BackupError(RuntimeError):
    """A backup could not be created, verified or restored safely."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The four own-task route fields are declared in secretary/secretary_models.py in
# the secretary/ subpackage. They are repeated here only as opaque key names so
# this backup helper stays inside the stdlib-only runtime package.
_ROUTE_KEYS = ("primary_provider", "primary_model",
               "fallback_provider", "fallback_model")


def _native_keys() -> tuple[str, ...]:
    from .userconfig import NATIVE_FLAT_FIELDS
    return (*NATIVE_FLAT_FIELDS, *_ROUTE_KEYS, "user_config")


def _read_native_values(native: Any) -> dict[str, Any]:
    """Read ONLY this plugin's declared setting keys through the supported reader."""
    if native is None:
        return {}
    getter = getattr(native, "get_config", None)
    if not callable(getter):
        if isinstance(native, Mapping):
            getter = native.get
        else:
            return {}
    values: dict[str, Any] = {}
    for key in _native_keys():
        try:
            value = getter(key, _UNSET)
        except Exception as exc:  # noqa: BLE001 - a failed snapshot must not write
            raise BackupError(f"cannot read native setting {key!r} ({type(exc).__name__})") from exc
        if value is not _UNSET:
            values[key] = copy.deepcopy(value)
    return values


def _read_onboarding(state: Any) -> Any:
    if state is None:
        return None
    getter = getattr(state, "get", None)
    if not callable(getter):
        return None
    try:
        raw = getter(ONBOARDING_STATE_KEY, None)
    except Exception as exc:  # noqa: BLE001 - a failed snapshot must not write
        raise BackupError(
            f"cannot read onboarding state ({type(exc).__name__})") from exc
    return copy.deepcopy(raw) if raw is not None else None


def _snapshot(data_root: Path, native: Any, state: Any) -> dict[str, bytes]:
    """Return ``{record name: exact bytes}`` for the records that exist.

    Missing records are omitted rather than invented, so a restore can never
    create a configuration that the owner did not have.
    """
    root = Path(data_root)
    records: dict[str, bytes] = {}
    for name in _FILE_RECORDS:
        path = root / name
        if path.is_file():
            records[name] = path.read_bytes()
    native_values = _read_native_values(native)
    if native_values:
        records[_NATIVE_SETTINGS_FILE] = (
            json.dumps(native_values, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    onboarding = _read_onboarding(state)
    if onboarding is not None:
        records[_ONBOARDING_FILE] = (
            json.dumps(onboarding, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    return records


def create_backup(data_root: str | Path, *, native: Any = None, state: Any = None,
                  reason: str = "", plugin: str = "hermes-proactive-secretary") -> dict[str, Any]:
    """Snapshot the plugin's own settings records into a new owner-only backup.

    Raises :class:`BackupError` on any failure. The caller must not perform the
    settings write when this raises.
    """
    root = Path(data_root)
    records = _snapshot(root, native, state)
    backups = root / BACKUP_DIRNAME
    try:
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backups, 0o700)
    except OSError as exc:
        raise BackupError(f"cannot create backup directory ({type(exc).__name__})") from exc
    stamp = _now().replace(":", "").replace("-", "")
    name = f"{stamp}-{uuid.uuid4().hex[:8]}"
    target = backups / name
    try:
        target.mkdir(mode=0o700)
    except OSError as exc:
        raise BackupError(f"cannot create backup {name} ({type(exc).__name__})") from exc
    entries = []
    for record, data in sorted(records.items()):
        path = target / record
        try:
            with open(path, "wb") as handle:
                handle.write(data)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise BackupError(
                f"cannot write backup record {record} ({type(exc).__name__})") from exc
        entries.append({"name": record, "sha256": _sha256(data), "bytes": len(data)})
    manifest = {
        "schema": SCHEMA,
        "plugin": plugin,
        "created_at": _now(),
        "reason": str(reason or "settings change")[:200],
        "data_root": str(root),
        "files": entries,
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    try:
        manifest_path = target / "manifest.json"
        with open(manifest_path, "wb") as handle:
            handle.write(manifest_bytes)
        os.chmod(manifest_path, 0o600)
        dir_fd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise BackupError(f"cannot write backup manifest ({type(exc).__name__})") from exc
    return {
        "created": True,
        "path": str(target),
        "manifest": str(target / "manifest.json"),
        "reason": manifest["reason"],
        "files": entries,
        "manifest_sha256": _sha256(manifest_bytes),
    }


def _load_manifest(backup_dir: str | Path) -> tuple[Path, dict[str, Any]]:
    path = Path(backup_dir)
    manifest_path = path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"cannot read backup manifest ({type(exc).__name__})") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise BackupError("backup manifest has an unsupported schema")
    return path, manifest


def verify_backup(backup_dir: str | Path) -> dict[str, Any]:
    """Verify every recorded file exists and still matches its manifest hash."""
    path, manifest = _load_manifest(backup_dir)
    problems: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BackupError("backup manifest has no file list")
    for entry in files:
        if not isinstance(entry, dict):
            problems.append("malformed manifest entry")
            continue
        name = str(entry.get("name") or "")
        if name not in _ALLOWED_FILES:
            problems.append(f"unexpected backup record {name!r}")
            continue
        record = path / name
        try:
            data = record.read_bytes()
        except OSError:
            problems.append(f"missing backup record {name!r}")
            continue
        if _sha256(data) != entry.get("sha256"):
            problems.append(f"hash mismatch for {name!r}")
        if len(data) != entry.get("bytes"):
            problems.append(f"size mismatch for {name!r}")
    return {"ok": not problems, "path": str(path), "problems": problems,
            "manifest": manifest}


def list_backups(data_root: str | Path) -> list[dict[str, Any]]:
    """List verifiable-or-not backups newest first; never returns file contents."""
    backups = Path(data_root) / BACKUP_DIRNAME
    if not backups.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(backups.iterdir(), reverse=True):
        if not child.is_dir() or not (child / "manifest.json").is_file():
            continue
        try:
            verified = verify_backup(child)
            rows.append({"path": str(child), "ok": verified["ok"],
                         "problems": verified["problems"],
                         "created_at": verified["manifest"].get("created_at", ""),
                         "reason": verified["manifest"].get("reason", "")})
        except BackupError as exc:
            rows.append({"path": str(child), "ok": False, "problems": [str(exc)]})
    return rows


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


def restore_backup(backup_dir: str | Path, data_root: str | Path, *,
                   native: Any = None, state: Any = None) -> dict[str, Any]:
    """Restore a verified backup to this plugin's records only.

    The caller supplies the host context/state facade when native settings or
    onboarding state must be restored; without the supported writer a restore
    fails closed rather than silently restoring only part of the snapshot.
    """
    path, manifest = _load_manifest(backup_dir)
    verified = verify_backup(path)
    if not verified["ok"]:
        raise BackupError("refusing to restore an unverified backup: "
                          + "; ".join(verified["problems"]))
    root = Path(data_root)
    restored: list[str] = []
    for entry in manifest.get("files") or []:
        name = str(entry.get("name") or "")
        data = (path / name).read_bytes()
        if name in _FILE_RECORDS:
            _write_private(root / name, data)
            restored.append(name)
        elif name == _NATIVE_SETTINGS_FILE:
            writer = getattr(native, "set_config", None)
            if not callable(writer):
                raise BackupError("native settings writer unavailable for restore")
            values = json.loads(data.decode("utf-8"))
            if not isinstance(values, dict):
                raise BackupError("native settings backup is not an object")
            for key, value in values.items():
                writer(key, copy.deepcopy(value))
            restored.append(name)
        elif name == _ONBOARDING_FILE:
            setter = getattr(state, "set", None)
            if not callable(setter):
                raise BackupError("onboarding state writer unavailable for restore")
            setter(ONBOARDING_STATE_KEY, json.loads(data.decode("utf-8")))
            restored.append(name)
    return {"ok": True, "restored": restored, "path": str(path)}


def remove_partial_backup(path: str | Path) -> None:
    """Best-effort cleanup of an incomplete backup made by this module."""
    target = Path(path)
    try:
        shutil.rmtree(target)
    except OSError:
        pass


__all__ = [
    "SCHEMA", "BACKUP_DIRNAME", "BackupError", "create_backup", "verify_backup",
    "list_backups", "restore_backup",
]
