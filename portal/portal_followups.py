"""Typed foreground portal for the bounded follow-up lifecycle store.

The durable lifecycle itself lives in :mod:`proactive_pilot.followups`. This
module is the thin authority seam:

* ``create``/``list``/``inspect``/``stop`` are foreground-only. They require the
  exact current human turn for the same session, which the existing registered
  ``pre_llm_call`` hook stored; the model cannot invent a quote, use a background
  or shared turn, or reuse a stale/foreign session.
* ``due``/``record_check``/``queue_notice``/``resolve_notice`` are portal-only.
  They require the same verified owned native portal execution identity used by
  ``portal_propose_notice`` before any follow-up or notice state is written.
* Notice proposals go through the existing gated ``portal_notices`` transport.
  A one-shot follow-up is frozen durably (same stable event key/id) before the
  enqueue attempt, so a restart can never create a second send path. An
  ambiguous enqueue is reported as frozen/unknown, never blindly retried and
  never called delivered.
* An emitted-unconfirmed one-shot may be closed by an exact owner receipt:
  the reference is derived only from the real per-notice queue evidence, and
  the owner must acknowledge that exact reference in the current private turn.
  This records a distinct owner-confirmed outcome, frees the pending slot and
  never claims transport delivery or resends.
* The Desktop native-approval fallback for create/stop remains closed. The safe
  approval seam is available to other foreground flows, but this surface has no
  proof that it yields a positive interactive per-request owner decision bound
  to the exact follow-up operation/profile/payload (rather than ambient
  approval mode, an "always" grant, a synthetic turn, a quote, a claimed
  platform or a model argument). Until that host contract is proven, ordinary
  authenticated owner-turn binding is the only create/stop authority here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

try:  # package import (installed host) or top-level test import
    from .. import paths
    from . import portal_responsibilities as responsibilities
except ImportError:  # pragma: no cover - direct module execution
    import paths  # type: ignore[no-redef]
    from portal import portal_responsibilities as responsibilities  # type: ignore[no-redef]

TOOL_NAME = "portal_followup"
MAX_ID = 96
MAX_QUOTE = 2000
MAX_MESSAGE = 350
MAX_TOPIC = 80
MAX_EVIDENCE = 8
MAX_EVIDENCE_REF = 240
MAX_EXPIRY_TEXT = 64
MAX_RECEIPT_REF = 96
DEFAULT_MAX_ROWS = 15
RECEIPT_REF_PREFIX = "notice-receipt:"

# Producer receipts that prove this attempt wrote nothing to the portal queue.
# Anything else (including save_failed and exceptions) stays frozen.
_NO_ENQUEUE_STATUSES = frozenset({
    "invalid_identity", "invalid_proposal", "locked", "corrupt_queue",
    "execution_used", "queue_full",
})

FOREGROUND_ACTIONS = frozenset({"create", "list", "inspect", "stop", "confirm_receipt"})
PORTAL_ACTIONS = frozenset({"due", "record_check", "queue_notice", "resolve_notice"})
_ACTION_ALIASES = {
    "get": "inspect",
    "cancel": "stop",
    "remove": "stop",
    "check": "record_check",
    "notify": "queue_notice",
    "propose_notice": "queue_notice",
    "resolve": "resolve_notice",
    "rows": "due",
    "due_rows": "due",
    "confirm": "confirm_receipt",
    "acknowledge": "confirm_receipt",
    "acknowledge_receipt": "confirm_receipt",
}

_NON_HUMAN = frozenset({
    "", "cron", "scheduler", "a2a", "agent", "subagent", "internal", "system",
    "worker", "background",
})
_STOP_INTENT = re.compile(
    responsibilities._LIFECYCLE_INTENT.pattern
    + r"|\b(?:stop|close|forget|end|drop)\b",
    re.IGNORECASE,
)
_STOP_WORD = re.compile(r"\b(?:stop|cancel|close|remove|forget|done|end|drop)\b", re.IGNORECASE)
_RETRACTION = re.compile(
    r"(?:\b(?:don['\u2019]?t|do\s+not)\s+(?:(?:need|want)\s+to\s+)?"
    r"(?:watch|follow\s+up|keep\s+an\s+eye|monitor|track)\b"
    r"|\bno\s+need\s+to\s+(?:watch|follow\s+up|keep\s+an\s+eye|monitor|track)\b"
    r"|\bstop\s+(?:watching|following\s+up|monitoring|tracking)\b"
    r"|\bnever\s+mind\b|\bforget\s+it\b)",
    re.IGNORECASE,
)
_RECEIPT_ACK = re.compile(
    r"(?:\b(?:received|saw|seen|read|acknowledg(?:e|ed|ing))\b"
    r"|\bconfirm(?:ed|ing)?\s+(?:receipt|the\s+notice)\b"
    r"|\bgot\s+(?:the\s+)?(?:notice|alert|follow-?up|message|receipt)\b)",
    re.IGNORECASE,
)
_RECEIPT_DISCLAIM = re.compile(
    r"\b(?:did\s+not|didn't|do\s+not|don't|have\s+not|haven't|has\s+not|hasn't|"
    r"not\s+(?:yet\s+)?(?:received|seen|read|got))\b",
    re.IGNORECASE,
)


def _clean(value: Any, limit: int = 400) -> str:
    return responsibilities._clean(value, limit)


def _store() -> Any:
    from proactive_pilot.clock import Clock
    from proactive_pilot.state import StateStore

    return StateStore(paths.state_dir(), Clock())


def _followups() -> Any:
    from proactive_pilot import followups

    return followups


def _refusal(status: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "status": status, "reason": str(reason)[:300]}


def _cron_execution(ctx: Any) -> Any:
    getter = getattr(ctx, "current_cron_execution", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 - an unreadable execution is not foreground
        return object()


def _require_foreground(ctx: Any) -> None:
    if ctx is None:
        raise ValueError("a foreground conversation context is required")
    execution = _cron_execution(ctx)
    if execution is not None:
        raise ValueError("create/list/inspect/stop are foreground-only")


def _turn_is_private_human(turn: Mapping[str, Any]) -> bool:
    platform = str(turn.get("platform") or "").strip().lower()
    chat_type = str(turn.get("chat_type") or "").strip().lower()
    if platform in _NON_HUMAN:
        return False
    if any(token in platform for token in ("cron", "a2a", "subagent", "internal")):
        return False
    if any(token in platform for token in ("group", "channel", "shared")):
        return False
    if chat_type in {"group", "channel", "supergroup", "shared"}:
        return False
    return True


def _current_turn(ctx: Any, args: Mapping[str, Any], *, stop: bool = False,
                  intent_gate: bool = True) -> dict[str, Any]:
    session_id = _clean(args.get("session_id"), 200)
    if not session_id:
        raise ValueError("session_id is required")
    turn = responsibilities.latest_turn(ctx, session_id=session_id)
    if turn is None:
        raise ValueError("no current human conversation turn for this session")
    if not _turn_is_private_human(turn):
        raise ValueError("a background or shared turn cannot create or stop a follow-up")
    quote = args.get("evidence_quote")
    if not responsibilities._quote_matches(quote, turn):
        raise ValueError("evidence_quote is not present in the current human turn")
    text = _clean(turn.get("text"), MAX_QUOTE)
    if responsibilities._QUOTED.search(text) or responsibilities._REPORTED.search(text):
        raise ValueError("quoted or reported text cannot authorise a follow-up action")
    if intent_gate:
        checked = _STOP_WORD.sub(" ", text) if stop else text
        if responsibilities._NEGATION.search(checked) or responsibilities._CONDITIONAL.search(checked):
            raise ValueError("a negated or conditional turn cannot authorise a follow-up action")
    return turn


_BINDING_STOP_WORDS = frozenset({
    "about", "again", "all", "and", "anything", "are", "been", "but", "can",
    "cancel", "check", "checking", "close", "could", "done", "drop", "end",
    "everything", "eye", "follow", "followup", "forget", "from", "have", "into",
    "just", "keep", "look", "monitor", "need", "notify", "now", "out", "please",
    "stop", "tell", "that", "the", "then", "this", "track", "want", "watch",
    "when", "with", "your",
})


def _binding_words(entry: Mapping[str, Any]) -> set[str]:
    candidate = " ".join(str(entry.get(key) or "") for key in ("text", "check_hint"))
    words = {word for word in re.findall(r"[a-z0-9]{4,}", candidate.lower())
             if word not in _BINDING_STOP_WORDS}
    ignored = getattr(responsibilities, "_STOP_WORDS", frozenset())
    return {word for word in words if word not in ignored}


def _resolve_stop_target(store: Any, text: str) -> tuple[dict[str, Any] | None, str]:
    """Resolve one active/awaiting stop target in this profile, or ask once."""
    F = _followups()
    candidates = F.active_entries(store) + F.awaiting_entries(store)
    lowered = text.lower()
    exact = {
        str(row.get("id") or ""): row
        for row in candidates
        if row.get("id") and str(row.get("id")).lower() in lowered
    }
    if exact:
        if len(exact) != 1:
            return None, "which follow-up do you mean? More than one exact id appears"
        return next(iter(exact.values())), ""
    matches = {
        str(row.get("id") or ""): row
        for row in candidates
        if any(re.search(rf"\b{re.escape(word)}\b", lowered)
               for word in _binding_words(row))
    }
    if len(matches) == 1:
        return next(iter(matches.values())), ""
    if not matches:
        return None, ("no active follow-up matches; name its id or a distinctive "
                      "word from it")
    return None, "which follow-up do you mean? Name its id or a distinctive word from it"


def _portal_identity(ctx: Any, store: Any) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Reuse the existing verified owned portal execution identity."""
    try:
        from .portal_portal_notices import _host_identity
    except ImportError:  # pragma: no cover - direct module execution
        from portal.portal_portal_notices import _host_identity  # type: ignore[no-redef]
    identity, refusal = _host_identity(ctx)
    if refusal is not None:
        return None, refusal
    try:
        expected = Path(paths.state_dir())
        actual = Path(getattr(store, "root", ""))
        if actual.resolve(strict=False) != expected.resolve(strict=False):
            return None, _refusal(
                "profile_mismatch", "follow-up store root does not match the active profile")
    except Exception:  # noqa: BLE001 - an unprovable profile fails closed
        return None, _refusal(
            "profile_mismatch", "follow-up store root cannot be verified")
    return identity, None


def _proposal(args: Mapping[str, Any], entry: Mapping[str, Any],
              now: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Build a proposal only from explicit caller-supplied notice fields.

    The watch text and watch id are never reused as the message or as evidence:
    a missing message or evidence ref means there is nothing trustworthy to
    enqueue yet, so the attempt is refused before any freeze or transport write.
    """
    text = args.get("message")
    if not isinstance(text, str) or not text.strip():
        return None, _refusal(
            "invalid_proposal",
            "an explicit non-empty message is required before enqueue",
        )
    if len(text) > MAX_MESSAGE:
        return None, _refusal(
            "invalid_proposal", f"message is too long (max {MAX_MESSAGE} chars)")

    evidence = args.get("evidence_refs")
    if not isinstance(evidence, list) or not evidence:
        return None, _refusal(
            "invalid_proposal",
            "explicit non-empty source evidence_refs are required before enqueue",
        )
    if len(evidence) > MAX_EVIDENCE:
        return None, _refusal(
            "invalid_proposal", f"evidence_refs may contain at most {MAX_EVIDENCE} items")
    watch_ref = f"followup:{entry['id']}"
    refs: list[str] = []
    for ref in evidence:
        if not isinstance(ref, str) or not ref.strip():
            return None, _refusal(
                "invalid_proposal", "each evidence ref must be a non-empty string")
        if len(ref) > MAX_EVIDENCE_REF:
            return None, _refusal(
                "invalid_proposal", f"each evidence ref must be at most {MAX_EVIDENCE_REF} chars")
        if any(ord(char) < 32 or ord(char) == 127 for char in ref):
            return None, _refusal(
                "invalid_proposal", "evidence refs must not contain control characters")
        if ref.strip() in {entry["id"], watch_ref}:
            return None, _refusal(
                "invalid_proposal",
                "a watch id is not source evidence; supply a real source reference")
        refs.append(ref)

    topic = args.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        topic = "Follow-up"
    expiry = args.get("expiry")
    if not isinstance(expiry, str) or not expiry.strip():
        expiry = (now + timedelta(hours=1)).isoformat()
    return {
        "event_key": _followups().notice_event_key(
            entry["id"], args.get("fingerprint") if entry.get("kind") == "ongoing" else ""),
        "topic": topic,
        "message": text,
        "evidence_refs": refs,
        "expiry": expiry,
    }, None


def _queue_evidence(store: Any, event_key: str) -> str:
    """Return the durable portal-queue state for one event key, if any.

    This is a read-only check used only to avoid calling a frozen attempt
    ``already_queued`` when no queue item or seen receipt exists.  A missing,
    corrupt or unreadable queue is treated as no evidence, never as a queue.
    """
    try:
        from proactive_pilot import portal_notices as transport
        queue = transport._load_queue(store)
    except Exception:  # noqa: BLE001 - unprovable queue is not evidence
        return ""
    for item in queue.get("pending") or []:
        if isinstance(item, dict) and item.get("event_key") == event_key:
            return "pending"
    seen = queue.get("seen")
    if isinstance(seen, dict) and event_key in seen:
        return "seen"
    return ""


def _portal_queue(store: Any) -> Mapping[str, Any] | None:
    """Read the existing portal queue only; never mutate or create it."""
    try:
        from proactive_pilot import portal_notices as transport
        queue = transport._load_queue(store)
    except Exception:  # noqa: BLE001 - unprovable evidence is not evidence
        return None
    return queue if isinstance(queue, Mapping) else None


def _owner_receipt(store: Any, entry: Mapping[str, Any],
                   queue: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Return the exact receipt ref only for real emitted-unconfirmed evidence.

    A reference is derived from the durable follow-up id, its stable notice event
    key and the per-notice ledger timestamp.  It is exposed only while the queue
    no longer holds the item pending and the ``seen`` ledger says exactly
    ``emitted-unconfirmed``.  Merely queued, expired, blocked or unknown states
    have no receipt reference.
    """
    if entry.get("kind") != "one_shot" or entry.get("status") != "awaiting_delivery":
        return None
    event_key = str(entry.get("notice_event_key") or "")
    if not event_key:
        return None
    if queue is None:
        queue = _portal_queue(store)
    if not isinstance(queue, Mapping):
        return None
    pending = queue.get("pending")
    if isinstance(pending, list) and any(
            isinstance(item, Mapping) and item.get("event_key") == event_key
            for item in pending):
        return None
    seen = queue.get("seen")
    record = seen.get(event_key) if isinstance(seen, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    if record.get("status") != "emitted-unconfirmed":
        return None
    emitted_at = record.get("at")
    if not isinstance(emitted_at, str) or not emitted_at:
        return None
    raw = f"{entry.get('id')}|{event_key}|{emitted_at}"
    ref = RECEIPT_REF_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return {
        "ref": ref,
        "event_key": event_key,
        "status": "emitted-unconfirmed",
        "emitted_at": emitted_at,
    }


def _with_owner_receipt(entry: Mapping[str, Any], store: Any,
                        queue: Mapping[str, Any] | None = None) -> dict[str, Any]:
    row = dict(entry)
    receipt = _owner_receipt(store, entry, queue=queue)
    if receipt is not None:
        row["owner_receipt"] = receipt
    return row


def _confirm_owner_receipt(args: Mapping[str, Any], ctx: Any,
                           store: Any, now: Any) -> dict[str, Any]:
    """Close one awaiting one-shot only from an exact owner receipt turn."""
    F = _followups()
    fid = _clean(args.get("followup_id"), MAX_ID)
    if not fid:
        raise ValueError("followup_id is required")
    claimed = args.get("notice_ref")
    if not isinstance(claimed, str) or not claimed.strip():
        raise ValueError("the exact notice receipt reference is required")
    claimed = claimed.strip()
    if (len(claimed) > MAX_RECEIPT_REF
            or any(ord(char) < 32 or ord(char) == 127 for char in claimed)):
        raise ValueError("notice_ref is malformed")

    turn = _current_turn(ctx, args)
    text = _clean(turn.get("text"), MAX_QUOTE)
    if _RECEIPT_DISCLAIM.search(text):
        raise ValueError("a negative or uncertain reply cannot confirm a receipt")
    if not _RECEIPT_ACK.search(text):
        raise ValueError(
            "confirming a notice receipt requires explicit receipt acknowledgement")
    if not re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(claimed)}(?![A-Za-z0-9_-])", text):
        raise ValueError(
            "the exact notice receipt reference is not in the current human turn")

    entry = F.get(store, fid)
    if (entry.get("status") == "done"
            and entry.get("close_reason") == "owner_confirmed"):
        if not hmac.compare_digest(
                str(entry.get("owner_confirmed_ref") or ""), claimed):
            return _refusal(
                "receipt_mismatch",
                "a different owner receipt reference is already recorded")
        return {
            "ok": True,
            "status": "already_owner_confirmed",
            "followup": entry,
            "delivery": "owner_confirmed_not_transport_verified",
            "note": ("This exact owner receipt was already recorded; nothing was resent."),
        }

    receipt = _owner_receipt(store, entry)
    if receipt is None:
        return _refusal(
            "receipt_not_available",
            "no emitted-unconfirmed notice evidence is bound to this follow-up")
    if not hmac.compare_digest(str(receipt["ref"]), claimed):
        return _refusal(
            "receipt_mismatch",
            "the supplied reference is not the exact receipt for this notice")
    try:
        confirmed = F.confirm_owner_receipt(
            store, fid, entry.get("notice_event_key"), claimed, now=now)
    except F.FollowupError as exc:
        return _refusal("invalid_state", str(exc))
    return {
        "ok": True,
        "status": "owner_confirmed",
        "followup": confirmed,
        "delivery": "owner_confirmed_not_transport_verified",
        "note": ("The owner's exact acknowledgment was recorded for this notice. This is "
                 "an owner-confirmed outcome, not a transport delivery receipt; the pending "
                 "slot is freed and the notice is not resent."),
    }


def _foreground_dispatch(action: str, args: Mapping[str, Any], ctx: Any,
                         store: Any, now: Any) -> dict[str, Any]:
    F = _followups()
    if action == "confirm_receipt":
        return _confirm_owner_receipt(args, ctx, store, now)
    if action == "create":
        request = _clean(args.get("request"), MAX_QUOTE)
        quote = _clean(args.get("evidence_quote"), MAX_QUOTE)
        if not quote and not request:
            raise ValueError(
                "nothing to file: quote the current human turn or pass an explicit request")
        turn = None
        if quote:
            turn = _current_turn(ctx, args, intent_gate=False)
            human_text = _clean(turn.get("text"), MAX_QUOTE)
            if _RETRACTION.search(human_text):
                raise ValueError(
                    "the current human turn retracts or declines a watch; nothing was filed")
        criterion = args.get("text")
        if not _clean(criterion, MAX_QUOTE):
            criterion = request
        kind = str(args.get("kind") or "ongoing").strip().lower()
        followup = F.create(
            store,
            criterion,
            kind=kind,
            source=_clean(turn.get("platform"), 24) if turn is not None else "",
            check_hint=args.get("check_hint"),
            evidence_quote=human_text if turn is not None else "",
            source_ref=_clean(turn.get("ref"), F.MAX_SOURCE_REF_CHARS)
            if turn is not None else "",
            filed_by="owner" if turn is not None else "agent",
            now=now,
        )
        return {
            "ok": True,
            "status": "created",
            "followup": followup,
            "source_ref": followup.get("source_ref", ""),
            "note": ("Created from the current private human turn or an explicit request; "
                     "no schedule or cron job was created. This is a small, reversible "
                     "text addition and can be removed with action=\"stop\"."),
        }
    if action == "list":
        queue = _portal_queue(store)
        awaiting = [
            _with_owner_receipt(row, store, queue=queue)
            for row in F.awaiting_entries(store)
        ]
        return {
            "ok": True,
            "status": "ok",
            "followups": F.active_entries(store),
            "awaiting_delivery": awaiting,
            "summary": F.summary(store),
        }
    if action == "inspect":
        entry = F.get(store, args.get("followup_id"))
        return {"ok": True, "status": "ok",
                "followup": _with_owner_receipt(entry, store)}
    # Asymmetry by design: filing is a small, reversible text addition, so
    # creation is agent-inferred with only a narrow retraction veto. Stopping is
    # destructive, so it keeps both explicit stop intent and exact-target
    # resolution; it is never relaxed to match create.
    turn = _current_turn(ctx, args, stop=True)
    text = _clean(turn.get("text"), MAX_QUOTE)
    if not _STOP_INTENT.search(text):
        raise ValueError("stopping a follow-up requires explicit intent in the current turn")
    target, reason = _resolve_stop_target(store, text)
    if target is None:
        return _refusal("needs_clarification", reason)
    followup = F.close(store, target["id"], "owner_stopped", now=now)
    return {"ok": True, "status": "stopped", "followup": followup}


def _portal_dispatch(action: str, args: Mapping[str, Any], ctx: Any,
                     store: Any, now: Any) -> dict[str, Any]:
    F = _followups()
    identity, refusal = _portal_identity(ctx, store)
    if refusal is not None:
        return refusal

    if action == "due":
        rows = F.portal_rows(store, max_entries=int(args.get("max_entries") or DEFAULT_MAX_ROWS))
        return {"ok": True, "status": "ok", "followups": rows, "count": len(rows)}

    if action == "record_check":
        followup = F.record_check(
            store,
            args.get("followup_id"),
            args.get("result"),
            args.get("fingerprint"),
            now=now,
        )
        return {"ok": True, "status": "checked", "followup": followup}

    if action == "resolve_notice":
        status = str(args.get("notice_status") or args.get("status") or "").strip().lower()
        if status == "delivered":
            return _refusal(
                "unconfirmed",
                "no verified delivery receipt exists; emitted-unconfirmed is not delivered",
            )
        followup = F.resolve_notice(store, args.get("followup_id"), status, now=now)
        return {"ok": True, "status": "resolved", "followup": followup}

    fid = _clean(args.get("followup_id"), MAX_ID)
    if not fid:
        raise ValueError("followup_id is required")
    entry = F.get(store, fid)
    fingerprint = _clean(args.get("fingerprint"), F.MAX_FINGERPRINT_CHARS)
    if entry.get("kind") == "ongoing" and not fingerprint:
        raise ValueError("an ongoing follow-up notice requires a bounded fingerprint")
    event_key = F.notice_event_key(fid, fingerprint if entry.get("kind") == "ongoing" else "")
    if (entry.get("status") == "awaiting_delivery"
            and entry.get("notice_event_key") == event_key):
        if _queue_evidence(store, event_key):
            return {
                "ok": True,
                "status": "already_queued",
                "frozen": True,
                "followup": entry,
                "event_key": event_key,
                "note": ("The one-shot follow-up was already frozen for this stable event "
                         "and the portal queue already holds it; no second enqueue was "
                         "attempted."),
            }
        return {
            "ok": True,
            "status": "already_frozen",
            "frozen": True,
            "followup": entry,
            "event_key": event_key,
            "note": ("The one-shot follow-up is durably frozen for this stable event, but "
                     "no portal queue evidence exists; it is not reported as queued and "
                     "is not retried automatically."),
        }
    if entry.get("status") != "active":
        return _refusal(
            "invalid_state", f"cannot queue a notice while {entry.get('status')}")

    proposal, refusal = _proposal(args, entry, now)
    if refusal is not None:
        return refusal
    try:
        from proactive_pilot import portal_notices as transport
        clean, error = transport._validate_proposal(proposal, now)
    except Exception as exc:  # noqa: BLE001 - no transport, no freeze
        return _refusal("submit_failed", f"notice transport unavailable: {type(exc).__name__}")
    if clean is None:
        return _refusal("invalid_proposal", error)

    try:
        frozen = F.queue_notice(store, fid, event_key, fingerprint or None, now=now)
    except F.FollowupCapacityError as exc:
        return {
            "ok": False,
            "status": "deferred",
            "deferred": True,
            "frozen": False,
            "capacity": exc.capacity,
            "code": ("pending_limit" if exc.capacity == "pending"
                     else "active_limit"),
            "followup": entry,
            "event_key": event_key,
            "reason": str(exc),
            "note": ("No state was frozen and nothing was dispatched; the watch "
                     "remains active and can be checked or queued by a later wake."),
        }
    except F.FollowupError as exc:
        return _refusal("freeze_failed", str(exc))
    attempt_token = frozen.pop("attempt_token", "")

    try:
        receipt = transport.submit_notice(
            store, store.clock, identity=identity, proposal=proposal)
    except Exception as exc:  # noqa: BLE001 - ambiguous failure stays frozen
        return {
            "ok": False,
            "status": "enqueue_unknown",
            "frozen": True,
            "followup": frozen,
            "event_key": event_key,
            "reason": (f"enqueue outcome is ambiguous ({type(exc).__name__}); the follow-up "
                       "stays frozen and is not retried automatically"),
        }

    if not isinstance(receipt, dict):
        return {
            "ok": False,
            "status": "enqueue_unknown",
            "frozen": True,
            "followup": frozen,
            "event_key": event_key,
            "reason": ("enqueue returned an unreadable result; the follow-up stays frozen "
                       "and is not retried automatically"),
        }

    receipt_status = str(receipt.get("status") or "")
    if receipt.get("ok") and receipt.get("accepted"):
        return {
            "ok": True,
            "status": "queued",
            "frozen": True,
            "followup": frozen,
            "receipt": receipt,
            "event_key": event_key,
            "note": ("Queued for the existing notification gates; this is not delivery "
                     "and no receipt is invented."),
        }
    if receipt_status == "duplicate":
        if _queue_evidence(store, event_key):
            return {
                "ok": True,
                "status": "already_queued",
                "frozen": True,
                "followup": frozen,
                "receipt": receipt,
                "event_key": event_key,
                "note": ("The stable semantic event already existed in the portal queue; "
                         "no second send path was created."),
            }
        return {
            "ok": True,
            "status": "already_frozen",
            "frozen": True,
            "followup": frozen,
            "receipt": receipt,
            "event_key": event_key,
            "note": ("The producer reported a duplicate event, but no portal queue "
                     "evidence could be read; the follow-up remains frozen and is not "
                     "reported as queued."),
        }
    if receipt_status in _NO_ENQUEUE_STATUSES:
        try:
            released = F.release_notice(
                store, fid, event_key, fingerprint or None,
                attempt_token=attempt_token, now=now)
        except F.FollowupError:
            return {
                "ok": False,
                "status": "already_frozen",
                "frozen": True,
                "followup": frozen,
                "receipt": receipt,
                "event_key": event_key,
                "reason": (f"notice enqueue was refused ({receipt_status}) but the "
                           "follow-up is no longer available to release safely; it "
                           "stays frozen"),
            }
        print_fp = fingerprint or event_key
        released_seen = released.get("seen") or []
        if (released.get("notice_event_key") == event_key
                or print_fp in released_seen):
            # Reservation identity changed (or the watch is no longer active), so
            # it is not safe to restore it; stay frozen and never revive a stop.
            return {
                "ok": False,
                "status": "already_frozen",
                "frozen": True,
                "followup": released,
                "receipt": receipt,
                "event_key": event_key,
                "reason": (f"notice enqueue was refused ({receipt_status}) and the "
                           "reservation could not be safely released; the follow-up "
                           "stays frozen"),
            }
        return {
            "ok": False,
            "status": receipt_status,
            "frozen": False,
            "followup": released,
            "receipt": receipt,
            "event_key": event_key,
            "reason": (f"notice was not enqueued ({receipt_status}); the follow-up was "
                       "restored to active for a later wake"),
        }
    return {
        "ok": False,
        "status": receipt_status or "enqueue_failed",
        "frozen": True,
        "followup": frozen,
        "receipt": receipt,
        "event_key": event_key,
        "reason": ("the follow-up stays frozen because enqueue could not be proven; "
                   "do not retry blindly"),
    }


def dispatch(args: Mapping[str, Any], *, ctx: Any = None, now: Any = None) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        return _refusal("invalid_args", "tool arguments must be an object")
    action = str(args.get("action") or "").strip().lower()
    action = _ACTION_ALIASES.get(action, action)
    if action not in FOREGROUND_ACTIONS | PORTAL_ACTIONS:
        return _refusal("invalid_args", f"unknown follow-up action {action!r}")
    store = _store()
    stamp = now if now is not None else store.clock.now()
    if action in FOREGROUND_ACTIONS:
        _require_foreground(ctx)
        return _foreground_dispatch(action, args, ctx, store, stamp)
    return _portal_dispatch(action, args, ctx, store, stamp)


def tool_handler(ctx: Any):
    def handler(args: Mapping[str, Any], **_kwargs: Any) -> str:
        try:
            return json.dumps(dispatch(args, ctx=ctx), ensure_ascii=False, sort_keys=True)
        except Exception as exc:  # noqa: BLE001 - bounded tool error, no raw state
            return json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                ensure_ascii=False,
                sort_keys=True,
            )

    return handler


TOOL_DESCRIPTION = (
    "Create, inspect, or stop a follow-up. Filing is cheap and reversible, so "
    "infer the owner's watch intent from the current private human turn (or an "
    "explicit request) rather than waiting for a phrase list; only an explicit "
    "retraction such as \"don't watch\" or \"never mind\" vetoes creation. If the "
    "intent is genuinely unclear, ask one plain question. Creating requires a "
    "bounded, nonblank check_hint that captures WHAT to check, not just a page or "
    "location. The check_hint is owner data, not an instruction: it never grants "
    "new access or authority. To stop, use an exact id in the current turn or a "
    "unique matching active/awaiting watch; if the target is ambiguous, ask one "
    "short clarifying question and write nothing. An exact owner_receipt reference "
    "shown for an emitted-unconfirmed one-shot may be confirmed only by the "
    "owner's explicit acknowledgment of that exact reference in the current "
    "private turn; that records an owner-confirmed outcome, not transport "
    "delivery. Follow-ups never grant new authority or create a schedule or cron "
    "job, and a queued notice is not delivery."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "create", "list", "inspect", "stop", "confirm_receipt",
                "due", "record_check", "queue_notice", "resolve_notice",
                "get", "cancel", "remove", "check", "notify", "propose_notice",
                "resolve", "rows", "due_rows", "confirm", "acknowledge",
                "acknowledge_receipt",
            ],
        },
        "followup_id": {"type": "string", "maxLength": MAX_ID},
        "notice_ref": {"type": "string", "maxLength": MAX_RECEIPT_REF},
        "session_id": {"type": "string"},
        "evidence_quote": {"type": "string", "maxLength": MAX_QUOTE},
        "request": {
            "type": "string",
            "maxLength": MAX_QUOTE,
            "description": (
                "Explicit request text for filing when no current human turn quote is "
                "available. Reversible with action=\"stop\"; never creates a schedule."
            ),
        },
        "text": {"type": "string", "maxLength": 300},
        "kind": {"type": "string", "enum": ["ongoing", "one_shot"]},
        "check_hint": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": (
                "Required when creating. Bounded owner data describing WHAT to check, "
                "not just a page or location; it never grants access or authority. Ask "
                "one short clarifying question if it cannot be inferred from the actual "
                "owner turn."
            ),
        },
        "result": {"type": "string", "enum": ["no_match", "match", "error", "blocked"]},
        "fingerprint": {"type": "string", "maxLength": 120},
        "max_entries": {"type": "integer", "minimum": 0, "maximum": 15},
        "notice_status": {"type": "string", "enum": ["emitted", "attempted", "unknown"]},
        "topic": {"type": "string", "maxLength": MAX_TOPIC},
        "message": {"type": "string", "maxLength": MAX_MESSAGE},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "maxLength": MAX_EVIDENCE_REF},
            "minItems": 1,
            "maxItems": MAX_EVIDENCE,
        },
        "expiry": {"type": "string", "format": "date-time", "maxLength": MAX_EXPIRY_TEXT},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def register_tool(ctx: Any, *, register_tool: Any = None) -> dict[str, Any]:
    register = register_tool if register_tool is not None else getattr(ctx, "register_tool", None)
    if not callable(register):
        return {"followup_tool": False, "followup_tool_error": "register_tool unavailable"}
    try:
        register(
            TOOL_NAME,
            "portal",
            TOOL_SCHEMA,
            tool_handler(ctx),
            description=TOOL_DESCRIPTION,
            emoji="",
        )
        return {"followup_tool": True}
    except Exception as exc:  # noqa: BLE001 - registration stays optional
        return {"followup_tool": False,
                "followup_tool_error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "TOOL_NAME", "TOOL_SCHEMA", "TOOL_DESCRIPTION", "dispatch", "tool_handler",
    "register_tool",
]
