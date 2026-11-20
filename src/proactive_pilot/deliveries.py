"""Bounded, owner-only delivery ledger for proactive notices.

Scope: portal notices and the ordinary monitoring alert lane. Requested-reminder
dispatch (``reminders.py``) is deliberately not ledgered in this release.

The ledger exists so a later interactive turn can resolve a deictic reference to
a notice delivered earlier in the same profile, without changing the delivered
message at all. It stores the exact emitted text only on the owner-only private
state volume; the injected conversation context is always content-free because
the host exposes no chat discriminator at ``pre_llm_call``.

Storage follows :mod:`proactive_pilot.state`:
- directory 0700 and file 0600 through :class:`StateStore`;
- atomic temp-file write plus ``os.replace``;
- a dedicated non-blocking ``deliveries.lock``;
- strict reads refuse symlinked or malformed artifacts.

A ledger failure is diagnostic only: recording never raises into a delivery
path and a corrupt store never suppresses a notice.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .state import CorruptStateError

SCHEMA = 1
STORE = "deliveries.json"
LOCK = "deliveries.lock"

MAX_ROWS = 50
RETENTION_DAYS = 14
MAX_MESSAGE_CHARS = 350
MAX_EVIDENCE_REFS = 8
MAX_EVIDENCE_CHARS = 240
MAX_KIND_CHARS = 64
MAX_EVENT_KEY_CHARS = 200
MAX_NOTICE_ID_CHARS = 96
MAX_JOB_ID_CHARS = 128
MAX_JOB_NAME_CHARS = 160
MAX_DESTINATION_CHARS = 160
MAX_STATUS_CHARS = 64


class DeliveryLedgerError(ValueError):
    """Invalid ledger input or an unreadable strict ledger."""


def empty_store() -> dict[str, Any]:
    return {"schema": SCHEMA, "version": 0, "updated_at": "", "rows": []}


def _report(message: str) -> None:
    """Diagnostic only; never let a ledger problem break delivery."""
    try:
        sys.stderr.write(f"proactive-secretary: delivery ledger: {message}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        pass


def _root(store: Any) -> Path:
    root = getattr(store, "root", None)
    if root is None:
        raise DeliveryLedgerError("state store has no root")
    return Path(root)


def _refuse_symlinks(root: Path) -> None:
    for path in (root, root / STORE, root / LOCK):
        if path.is_symlink():
            name = path.name or str(path)
            raise CorruptStateError(f"refusing symlinked delivery ledger path: {name}")


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _evidence(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        values = [values]
    try:
        items = list(values)
    except TypeError:
        return []
    out: list[str] = []
    for value in items:
        text = _text(value, MAX_EVIDENCE_CHARS)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_EVIDENCE_REFS:
            break
    return out


def _parse_at(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CorruptStateError("delivery row has no ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CorruptStateError("delivery row has an invalid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise CorruptStateError("delivery row timestamp is not timezone-aware")
    return parsed


def _validate_row(row: Any) -> None:
    if not isinstance(row, dict):
        raise CorruptStateError("delivery row is not an object")
    _parse_at(row.get("at"))
    checks = (
        ("kind", MAX_KIND_CHARS),
        ("message", MAX_MESSAGE_CHARS),
        ("event_key", MAX_EVENT_KEY_CHARS),
        ("notice_id", MAX_NOTICE_ID_CHARS),
        ("job_id", MAX_JOB_ID_CHARS),
        ("job_name", MAX_JOB_NAME_CHARS),
        ("destination", MAX_DESTINATION_CHARS),
        ("status", MAX_STATUS_CHARS),
    )
    for key, limit in checks:
        value = row.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            raise CorruptStateError(f"delivery row {key} is invalid")
    evidence = row.get("evidence_refs", [])
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_REFS:
        raise CorruptStateError("delivery row evidence_refs is invalid")
    for item in evidence:
        if not isinstance(item, str) or len(item) > MAX_EVIDENCE_CHARS:
            raise CorruptStateError("delivery row evidence ref is invalid")


def _validate_store(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise CorruptStateError("deliveries.json has an unsupported schema")
    version = data.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise CorruptStateError("deliveries.json has an invalid version")
    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise CorruptStateError("deliveries.json rows are malformed or oversized")
    for row in rows:
        _validate_row(row)
    data.setdefault("updated_at", "")
    return data


def _load(store: Any, *, strict: bool) -> dict[str, Any]:
    root = _root(store)
    try:
        _refuse_symlinks(root)
        data = store.load(STORE, empty_store(), strict=True)
        return _validate_store(data)
    except CorruptStateError:
        if strict:
            raise
    except Exception as exc:  # noqa: BLE001 - non-strict reads stay empty
        if strict:
            raise CorruptStateError(f"deliveries.json is unreadable: {exc}") from exc
    _report("unreadable store treated as empty")
    return empty_store()


def _prune(rows: list[dict[str, Any]], now: datetime) -> None:
    cutoff = now - timedelta(days=RETENTION_DAYS)
    kept: list[dict[str, Any]] = []
    for row in rows:
        try:
            at = _parse_at(row.get("at"))
        except CorruptStateError:
            continue
        if at >= cutoff:
            kept.append(row)
    rows[:] = kept


def _cap(rows: list[dict[str, Any]]) -> None:
    if len(rows) > MAX_ROWS:
        del rows[: len(rows) - MAX_ROWS]


def _duplicate(rows: Iterable[dict[str, Any]], event_key: str, notice_id: str) -> bool:
    for row in rows:
        if event_key and row.get("event_key") == event_key:
            return True
        if notice_id and row.get("notice_id") == notice_id:
            return True
    return False


def record_emission(
    store: Any,
    *,
    message: Any,
    kind: Any,
    event_key: Any = "",
    notice_id: Any = "",
    evidence_refs: Any = (),
    job_id: Any = "",
    job_name: Any = "",
    destination: Any = "",
    now: datetime | None = None,
    status: Any = "emitted-unconfirmed",
) -> None:
    """Record one notice that was actually handed to the delivery boundary.

    Never raises: any ledger problem is reported to stderr and the caller keeps
    emitting the unchanged message.
    """
    try:
        text = _text(message, MAX_MESSAGE_CHARS)
        if not text.strip():
            return
        stamp_dt = now if now is not None else store.clock.now()
        stamp = stamp_dt.isoformat()
        row = {
            "at": stamp,
            "kind": _text(kind, MAX_KIND_CHARS),
            "message": text,
            "event_key": _text(event_key, MAX_EVENT_KEY_CHARS),
            "notice_id": _text(notice_id, MAX_NOTICE_ID_CHARS),
            "evidence_refs": _evidence(evidence_refs),
            "job_id": _text(job_id, MAX_JOB_ID_CHARS),
            "job_name": _text(job_name, MAX_JOB_NAME_CHARS),
            "destination": _text(destination, MAX_DESTINATION_CHARS),
            "status": _text(status, MAX_STATUS_CHARS) or "emitted-unconfirmed",
        }
        root = _root(store)
        _refuse_symlinks(root)
        acquired = False
        try:
            with store.lock(LOCK) as got:
                acquired = bool(got)
                if not acquired:
                    _report("another writer holds deliveries.lock; skipped")
                    return
                data = _load(store, strict=False)
                rows = data["rows"]
                if _duplicate(rows, row["event_key"], row["notice_id"]):
                    return
                rows.append(row)
                _prune(rows, stamp_dt)
                _cap(rows)
                data["version"] = int(data.get("version", 0)) + 1
                data["updated_at"] = stamp
                store.save(STORE, data)
        except CorruptStateError as exc:
            _report(str(exc))
        except Exception as exc:  # noqa: BLE001 - delivery must not be blocked
            _report(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - absolutely no raise into delivery
        _report(f"{type(exc).__name__}: {exc}")


def _coerce_since(store: Any, since: Any) -> datetime | None:
    if since is None or since == "":
        return None
    if isinstance(since, datetime):
        parsed = since
    else:
        text = str(since).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DeliveryLedgerError(f"invalid ISO timestamp: {since!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=store.clock.now().tzinfo)
    return parsed


def recent(store: Any, *, since: Any = None, limit: int = 10,
           strict: bool = True) -> list[dict[str, Any]]:
    """Return newest-first bounded rows, optionally on/after ``since``."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise DeliveryLedgerError("limit must be a non-negative integer")
    data = _load(store, strict=strict)
    cutoff = _coerce_since(store, since)
    rows: list[dict[str, Any]] = []
    for row in data["rows"]:
        if cutoff is not None and _parse_at(row.get("at")) < cutoff:
            continue
        rows.append(dict(row))
    rows.sort(key=lambda item: str(item.get("at", "")), reverse=True)
    return rows[: min(limit, MAX_ROWS)]


def summary_since(store: Any, *, within_hours: int = 24) -> dict[str, Any] | None:
    """Content-free recency summary for the conversation hook.

    Returns only ``{"count", "newest"}`` (never notice text, handles or
    destinations) and degrades to ``None`` on any ledger problem.
    """
    try:
        now = store.clock.now()
        cutoff = now - timedelta(hours=max(0, int(within_hours)))
        data = _load(store, strict=False)
        newest: datetime | None = None
        count = 0
        for row in data["rows"]:
            at = _parse_at(row.get("at"))
            if at < cutoff:
                continue
            count += 1
            if newest is None or at > newest:
                newest = at
        if count <= 0 or newest is None:
            return None
        return {"count": count, "newest": newest.isoformat()}
    except Exception as exc:  # noqa: BLE001 - hook context never breaks a turn
        _report(f"{type(exc).__name__}: {exc}")
        return None


def emit_fingerprint(store: Any, *, within_hours: int = 24) -> str:
    """Stable short id over the content-free shape of recent emissions."""
    try:
        summary = summary_since(store, within_hours=within_hours)
        if not summary:
            return ""
        rows = recent(store, limit=MAX_ROWS, strict=False)
        raw = "|".join(
            f"{row.get('at','')}\x1f{row.get('kind','')}\x1f"
            f"{row.get('event_key','')}\x1f{row.get('notice_id','')}"
            for row in rows
        )
        raw = f"{summary['count']}\x1f{summary['newest']}\x1f{raw}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001 - optional fingerprint only
        _report(f"{type(exc).__name__}: {exc}")
        return ""


__all__ = [
    "SCHEMA", "STORE", "LOCK", "MAX_ROWS", "RETENTION_DAYS",
    "DeliveryLedgerError", "empty_store", "record_emission", "recent",
    "summary_since", "emit_fingerprint",
]
