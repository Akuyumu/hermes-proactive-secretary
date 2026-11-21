"""Owner-only pending-approval draft directories.

A draft is a staged external action that has deliberately **not** been
performed: an email, message, calendar entry, purchase, file or other effect.
The directory tree plus each ``manifest.json`` is the entire state. There is no
index, database or second bookkeeping store.

Layout under ``<plugin data root>/drafts``::

    pending/<draft_id>/{manifest.json,body.md}
    approved/<draft_id>/...
    discarded/<draft_id>/...
    expired/<draft_id>/...

A portal run may ``stage`` a pending draft; only a foreground owner session may
``approve`` or ``discard`` it. Approval moves the directory and returns a local
receipt. It never sends, schedules, purchases or otherwise executes the action.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import ownerpaths as _ownerpaths

SCHEMA = 1
STAGES = ("pending", "approved", "discarded", "expired")
DEFAULT_STAGE = "pending"
INTENTS = frozenset({"email", "message", "calendar", "purchase", "file", "other"})
FILED_BY = frozenset({"agent", "owner"})
MAX_DRAFT_ID = 24
MAX_TITLE = 120
MAX_SUBJECT = 200
MAX_TO = 200
MAX_REASON = 600
MAX_EVIDENCE = 5
MAX_EVIDENCE_REF = 240
MAX_BODY_BYTES = 131072
MAX_BODY_SHOW = 8000
MAX_PENDING = 10
MAX_STAGES_PER_EXECUTION = 3
MAX_EXPIRY_DAYS = 30
DEFAULT_EXPIRY_DAYS = 7
DEFAULT_BODY_REF = "body.md"
DUPLICATE_WINDOW = timedelta(hours=24)

_ID_RE = re.compile(r"^dr_[0-9a-f]{16}$")
_BODY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(?:md|txt)$")
_RAW_HTML = re.compile(r"<(?=[A-Za-z!?/])")
_ACTIVE_HTML = re.compile(
    r"<\s*(script|iframe|object|embed|form|meta|link|style|svg|math|img)\b|"
    r"on[a-z]+\s*=",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DraftError(ValueError):
    """Invalid draft input or an impossible lifecycle transition."""

    status = "invalid"


class DraftCapacityError(DraftError):
    """A bounded cap was reached before any write."""

    status = "capacity"

    def __init__(self, message: str, *, capacity: str) -> None:
        super().__init__(message)
        self.capacity = capacity


class DraftDuplicateError(DraftError):
    """The same intent/title/body was staged within the duplicate window."""

    status = "duplicate"


def data_root() -> Path:
    return Path(_ownerpaths.data_root())


def drafts_root(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else data_root() / "drafts"


def stage_dir(stage: str, root: Path | str | None = None) -> Path:
    if stage not in STAGES:
        raise DraftError("unknown draft stage")
    return drafts_root(root) / stage


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now().astimezone()


def _clean(value: Any, name: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise DraftError(f"{name} must be a string")
    text = " ".join(value.split())
    if required and not text:
        raise DraftError(f"{name} is required")
    if len(text) > limit:
        raise DraftError(f"{name} is too long (max {limit} chars)")
    if _CONTROL.search(text):
        raise DraftError(f"{name} contains control characters")
    return text


def _validate_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise DraftError("draft id is malformed")
    return text


def _parse_stamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DraftError(f"{name} is not an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DraftError(f"{name} is not an ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DraftError(f"{name} must include a UTC offset")
    return parsed


def _expiry(value: Any, stamp: datetime) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return (stamp + timedelta(days=DEFAULT_EXPIRY_DAYS)).isoformat()
    if not isinstance(value, str):
        raise DraftError("expires_at must be a string")
    text = value.strip()
    parsed = _parse_stamp(text, "expires_at")
    if parsed > stamp + timedelta(days=MAX_EXPIRY_DAYS):
        raise DraftError(
            f"expires_at is too far in the future (max {MAX_EXPIRY_DAYS} days)")
    return text


def _refuse_symlink(path: Path) -> None:
    if os.path.islink(str(path)):
        raise DraftError("refusing symlinked draft path")


def _reject_symlink_ancestors(path: Path) -> None:
    current = Path(path)
    for candidate in (current, *current.parents):
        if os.path.islink(str(candidate)):
            raise DraftError("refusing symlinked draft path component")


def _within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
        base = root.resolve(strict=True)
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def _ensure_dir(path: Path) -> Path:
    path = Path(path)
    _reject_symlink_ancestors(path)
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for component in reversed(missing):
        component.mkdir()
        os.chmod(component, 0o700)
    _reject_symlink_ancestors(path)
    os.chmod(path, 0o700)
    return path


def ensure_dirs(root: Path | str | None = None) -> Path:
    base = drafts_root(root)
    _reject_symlink_ancestors(base)
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    for stage in STAGES:
        _ensure_dir(base / stage)
    return base


def _atomic_write(path: Path, payload: bytes) -> None:
    _reject_symlink_ancestors(path.parent)
    _refuse_symlink(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        _refuse_symlink(path)
        os.replace(tmp_name, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    _atomic_write(path, payload)


def _read_manifest(path: Path) -> dict[str, Any]:
    _refuse_symlink(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DraftError(f"draft manifest cannot be read: {type(exc).__name__}") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise DraftError("draft manifest has an unsupported schema")
    _validate_id(data.get("id"))
    if data.get("status") not in STAGES:
        raise DraftError("draft manifest status is invalid")
    return data


def _manifest_path(folder: Path) -> Path:
    return folder / "manifest.json"


def _body_path(folder: Path, manifest: dict[str, Any]) -> Path:
    name = str(manifest.get("body_ref") or DEFAULT_BODY_REF)
    if not _BODY_NAME.fullmatch(name):
        raise DraftError("draft body reference is unsafe")
    return folder / name


def _iter_manifests(root: Path, stages: Iterable[str]) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    base = drafts_root(root)
    for stage in stages:
        folder_root = base / stage
        if not folder_root.is_dir() or os.path.islink(str(folder_root)):
            continue
        for folder in sorted(folder_root.iterdir()):
            if not folder.is_dir() or os.path.islink(str(folder)):
                continue
            if not _within(folder, base):
                continue
            manifest_file = _manifest_path(folder)
            if not manifest_file.is_file():
                continue
            try:
                found.append((folder, _read_manifest(manifest_file)))
            except DraftError:
                continue
    return found


def _locate(root: Path, draft_id: Any) -> tuple[str, Path, dict[str, Any]] | None:
    fid = _validate_id(draft_id)
    base = drafts_root(root)
    for stage in STAGES:
        folder = base / stage / fid
        if not folder.is_dir():
            continue
        if not _within(folder, base):
            raise DraftError("draft path escapes the drafts root")
        _reject_symlink_ancestors(folder)
        manifest = _read_manifest(_manifest_path(folder))
        if manifest.get("id") != fid:
            raise DraftError("draft identity does not match its directory")
        return stage, folder, manifest
    return None


def _move(folder: Path, target_stage: str, manifest: dict[str, Any],
          *, status: str, root: Path, stamp: datetime,
          resolution_note: str | None = None) -> tuple[Path, dict[str, Any]]:
    base = drafts_root(root)
    destination_root = base / target_stage
    _ensure_dir(destination_root)
    destination = destination_root / manifest["id"]
    if not _within(destination, base):
        raise DraftError("draft destination escapes the drafts root")
    if destination.exists():
        raise DraftError("draft destination already exists")
    updated = dict(manifest)
    updated["status"] = status
    if resolution_note is not None:
        updated["resolution_note"] = resolution_note
    _write_manifest(_manifest_path(folder), updated)
    os.replace(str(folder), str(destination))
    return destination, updated


def _pending_count(root: Path) -> int:
    return len(list((drafts_root(root) / "pending").glob("dr_*")))


def stage(
    root: Path | str | None = None,
    *,
    intent: Any,
    title: Any,
    body: Any,
    reason: Any,
    subject: Any = "",
    to: Any = "",
    evidence: Any = None,
    body_ref: Any = DEFAULT_BODY_REF,
    expires_at: Any = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one pending draft (portal authority); never executes anything."""
    if intent not in INTENTS:
        raise DraftError("intent must be one of: " + ", ".join(sorted(INTENTS)))
    clean_title = _clean(title, "title", MAX_TITLE, required=True)
    clean_subject = _clean(subject, "subject", MAX_SUBJECT)
    clean_to = _clean(to, "to", MAX_TO)
    clean_reason = _clean(reason, "reason", MAX_REASON, required=True)
    if not isinstance(body, str):
        raise DraftError("body must be text")
    payload = body.encode("utf-8")
    if len(payload) > MAX_BODY_BYTES:
        raise DraftError(f"body is too large (max {MAX_BODY_BYTES} bytes)")
    if _RAW_HTML.search(body) or _ACTIVE_HTML.search(body):
        raise DraftError("HTML payloads are refused; stage plain Markdown or text")
    clean_body_ref = str(body_ref or DEFAULT_BODY_REF).strip()
    if not _BODY_NAME.fullmatch(clean_body_ref):
        raise DraftError("body_ref must be a .md or .txt file name")
    refs = _clean_refs(evidence)
    stamp = _now(now)
    expiry = _expiry(expires_at, stamp)

    root_path = drafts_root(root)
    ensure_dirs(root_path)
    digest = hashlib.sha256(payload).hexdigest()

    for _folder, manifest in _iter_manifests(root_path, STAGES):
        if (manifest.get("intent") == intent
                and manifest.get("title") == clean_title
                and manifest.get("body_sha256") == digest):
            try:
                created = _parse_stamp(manifest.get("created_at"), "created_at")
            except DraftError:
                continue
            if stamp - created <= DUPLICATE_WINDOW:
                raise DraftDuplicateError(
                    "a duplicate intent/title/body draft was staged in the last 24 hours")

    if _pending_count(root_path) >= MAX_PENDING:
        raise DraftCapacityError(
            f"pending draft limit reached ({MAX_PENDING})", capacity="pending")

    while True:
        fid = "dr_" + secrets.token_hex(8)
        folder = root_path / DEFAULT_STAGE / fid
        if not folder.exists():
            break
    if not _within(folder, root_path):
        raise DraftError("draft path escapes the drafts root")
    _ensure_dir(folder)
    manifest = {
        "schema": SCHEMA,
        "id": fid,
        "intent": intent,
        "title": clean_title,
        "subject": clean_subject,
        "to": clean_to,
        "reason": clean_reason,
        "evidence": refs,
        "created_at": stamp.isoformat(),
        "expires_at": expiry,
        "status": "pending",
        "filed_by": "agent",
        "body_ref": clean_body_ref,
        "body_sha256": digest,
        "approved_at": "",
        "approved_by": "",
        "resolution_note": "",
    }
    try:
        _atomic_write(folder / clean_body_ref, payload)
        _write_manifest(_manifest_path(folder), manifest)
    except Exception:
        for child in folder.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass
        raise
    return manifest


def _clean_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise DraftError("evidence must be a list")
    refs: list[str] = []
    for item in value:
        text = _clean(item, "evidence ref", MAX_EVIDENCE_REF, required=True)
        if text not in refs:
            refs.append(text)
    if len(refs) > MAX_EVIDENCE:
        raise DraftError(f"evidence may contain at most {MAX_EVIDENCE} refs")
    return refs


def sweep_expired(root: Path | str | None = None, *,
                  now: datetime | None = None) -> list[str]:
    """Move pending drafts past ``expires_at`` into ``expired/``."""
    stamp = _now(now)
    base = drafts_root(root)
    moved: list[str] = []
    for folder, manifest in _iter_manifests(base, ("pending",)):
        try:
            expiry = _parse_stamp(manifest.get("expires_at"), "expires_at")
        except DraftError:
            continue
        if expiry >= stamp:
            continue
        _move(folder, "expired", manifest, status="expired",
              root=base, stamp=stamp, resolution_note="expired")
        moved.append(str(manifest.get("id") or ""))
    return moved


def list_pending(root: Path | str | None = None) -> list[dict[str, Any]]:
    """Portal readback: ids and titles only, never body text."""
    return [
        {"id": manifest.get("id"), "title": manifest.get("title")}
        for _folder, manifest in _iter_manifests(root, ("pending",))
    ]


def list_drafts(root: Path | str | None = None, *,
                now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """Foreground readback with expired pending items shown separately."""
    stamp = _now(now)
    result: dict[str, list[dict[str, Any]]] = {
        "pending": [], "approved": [], "discarded": [], "expired": [],
    }
    for folder, manifest in _iter_manifests(root, STAGES):
        stage_name = folder.parent.name
        try:
            expiry = _parse_stamp(manifest.get("expires_at"), "expires_at")
        except DraftError:
            expiry = None
        effective = stage_name
        if stage_name == "pending" and expiry is not None and expiry < stamp:
            effective = "expired"
        row = {
            "id": manifest.get("id"),
            "intent": manifest.get("intent"),
            "title": manifest.get("title"),
            "status": manifest.get("status"),
            "created_at": manifest.get("created_at"),
            "expires_at": manifest.get("expires_at"),
            "filed_by": manifest.get("filed_by"),
            "close_reason": (manifest.get("resolution_note")
                             if effective in {"expired", "discarded", "approved"}
                             else ""),
        }
        result[effective].append(row)
    return result


def show(root: Path | str | None = None, draft_id: Any = None, *,
         now: datetime | None = None) -> dict[str, Any]:
    del now  # the manifest's recorded state is returned; classification is not rewritten
    located = _locate(drafts_root(root), draft_id)
    if located is None:
        raise DraftError("no draft with that id")
    stage_name, folder, manifest = located
    body_file = _body_path(folder, manifest)
    _refuse_symlink(body_file)
    try:
        body = body_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise DraftError(f"draft body cannot be read: {type(exc).__name__}") from None
    return {
        "ok": True,
        "status": "ok",
        "stage": stage_name,
        "manifest": manifest,
        "body": body[:MAX_BODY_SHOW],
        "body_truncated": len(body) > MAX_BODY_SHOW,
        "draft_is_untrusted_data": True,
    }


def _receipt(folder: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "approval": "owner_confirmed_foreground",
        "executed": False,
        "receipt": manifest["id"],
        "intent": manifest.get("intent"),
        "to": manifest.get("to", ""),
        "subject": manifest.get("subject", ""),
        "body_ref": str(_body_path(folder, manifest)),
    }


def approve(root: Path | str | None = None, draft_id: Any = None, *,
            now: datetime | None = None) -> dict[str, Any]:
    """Move a pending draft to approved/ and return a local receipt only."""
    stamp = _now(now)
    base = drafts_root(root)
    located = _locate(base, draft_id)
    if located is None:
        raise DraftError("no draft with that id")
    stage_name, folder, manifest = located
    if stage_name == "approved":
        receipt = _receipt(folder, manifest)
        receipt["already"] = True
        return receipt
    if stage_name in {"discarded", "expired"}:
        return {"ok": False, "status": stage_name, "receipt": manifest["id"]}
    try:
        expiry = _parse_stamp(manifest.get("expires_at"), "expires_at")
    except DraftError:
        expiry = None
    if expiry is not None and expiry < stamp:
        _move(folder, "expired", manifest, status="expired",
              root=base, stamp=stamp, resolution_note="expired")
        return {"ok": False, "status": "expired", "receipt": manifest["id"]}

    updated = dict(manifest)
    updated["status"] = "approved"
    updated["approved_at"] = stamp.isoformat()
    updated["approved_by"] = "owner"
    _write_manifest(_manifest_path(folder), updated)
    destination, final = _move(
        folder, "approved", updated, status="approved", root=base, stamp=stamp)
    receipt = _receipt(destination, final)
    receipt["already"] = False
    return receipt


def discard(root: Path | str | None = None, draft_id: Any = None, *,
            note: Any = "", now: datetime | None = None) -> dict[str, Any]:
    """Move a pending draft to discarded/ with an optional bounded note."""
    stamp = _now(now)
    base = drafts_root(root)
    clean_note = _clean(note, "note", MAX_REASON)
    located = _locate(base, draft_id)
    if located is None:
        raise DraftError("no draft with that id")
    stage_name, folder, manifest = located
    if stage_name == "discarded":
        return {"ok": True, "status": "discarded",
                "receipt": manifest["id"], "already": True}
    if stage_name in {"approved", "expired"}:
        return {"ok": False, "status": stage_name, "receipt": manifest["id"]}
    try:
        expiry = _parse_stamp(manifest.get("expires_at"), "expires_at")
    except DraftError:
        expiry = None
    if expiry is not None and expiry < stamp:
        _move(folder, "expired", manifest, status="expired",
              root=base, stamp=stamp, resolution_note="expired")
        return {"ok": False, "status": "expired", "receipt": manifest["id"]}
    _move(folder, "discarded", manifest, status="discarded", root=base,
          stamp=stamp, resolution_note=clean_note)
    return {"ok": True, "status": "discarded", "receipt": manifest["id"],
            "already": False}


__all__ = [
    "SCHEMA", "STAGES", "INTENTS", "MAX_BODY_BYTES", "MAX_BODY_SHOW",
    "MAX_PENDING", "MAX_STAGES_PER_EXECUTION", "MAX_EXPIRY_DAYS",
    "DEFAULT_EXPIRY_DAYS", "DEFAULT_BODY_REF", "DraftError",
    "DraftCapacityError", "DraftDuplicateError", "drafts_root", "stage_dir",
    "ensure_dirs", "stage", "sweep_expired", "list_pending", "list_drafts",
    "show", "approve", "discard",
]
