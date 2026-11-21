"""One-shot, expiring approval questions for consequential proposals.

This module never performs the proposed external effect. It records one exact
proposal bound to the requesting human conversation and the current permission
policy, returns an opaque reference, and consumes the record only when the
current foreground human turn explicitly approves that same proposal.

The parser is deliberately conservative: negations, quoted/reported text,
conditional or conflicting replies, unrelated references, stale turns and a
different session/channel all deny. A plain affirmative without the exact opaque
reference in the real current human turn only asks for clarification; there is
no reply-context shortcut. Approval grants only the current proposal.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any, Mapping

APPROVALS_KEY = "secretary_approvals"
SCHEMA = 2
MAX_APPROVALS = 8
DEFAULT_TTL_SECONDS = 30 * 60
_REF = re.compile(r"\bsec-apr-[a-z0-9]{6,}\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KINDS = frozenset({"send", "publish", "purchase", "book", "calendar", "task",
                    "external", "other"})
_AFFIRMATIVE = re.compile(
    r"\b(?:yes|yeah|yep|yup|approve|approved|proceed|confirm|confirmed|ok|okay|"
    r"sure|go ahead|do it)\b", re.IGNORECASE)
_NEGATION = re.compile(
    r"\b(?:no|not|never|none|don't|dont|doesn't|does not|isn't|is not|won't|"
    r"will not|wouldn't|shouldn't|cannot|can't|cant|stop|cancel|decline|reject|"
    r"refuse|nope|nah|disapprove)\b", re.IGNORECASE)
_CONDITIONAL = re.compile(
    r"\b(?:if|unless|maybe|perhaps|possibly|might|but|however|though|although|"
    r"except|only|wait|hold|pending|after|before|until|provided|depends?)\b",
    re.IGNORECASE)
_QUOTED = re.compile(r'["“”`]|^\s*>', re.MULTILINE)
_REPORTED = re.compile(
    r"\b(?:said|says|say|saying|wrote|writes|write|quoted?|quoting|according)\b",
    re.IGNORECASE)
_ALLOWED_WORDS = frozenset({
    "yes", "yeah", "yep", "yup", "y", "approve", "approved", "proceed",
    "confirm", "confirmed", "ok", "okay", "sure", "please", "go", "ahead",
    "do", "it", "i", "certainly", "absolutely",
})
_STATE_LOCK = threading.RLock()


class ApprovalError(ValueError):
    pass


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return _CONTROL.sub("", text)[:limit]


def _facade(ctx: Any) -> Any:
    return getattr(ctx, "state", None) if ctx is not None else None


def _load(ctx: Any) -> list[dict[str, Any]]:
    facade = _facade(ctx)
    if facade is None:
        raise ApprovalError("durable conversation state is unavailable")
    try:
        raw = facade.get(APPROVALS_KEY)
    except Exception as exc:  # noqa: BLE001
        raise ApprovalError("approval state could not be read") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApprovalError("approval state is corrupt")
    rows: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            raise ApprovalError("approval state contains a corrupt row")
        rows.append(dict(row))
    return rows


def _save(ctx: Any, rows: list[dict[str, Any]]) -> None:
    facade = _facade(ctx)
    if facade is None:
        raise ApprovalError("durable conversation state is unavailable")
    try:
        facade.set(APPROVALS_KEY, rows[-MAX_APPROVALS:])
    except Exception as exc:  # noqa: BLE001
        raise ApprovalError("approval state could not be written") from exc


def _prune(rows: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    kept = []
    for row in rows:
        if row.get("status") != "pending":
            continue
        try:
            expires = float(row.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise ApprovalError("approval expiry is corrupt") from exc
        if not str(row.get("ref") or "").strip():
            raise ApprovalError("approval reference is corrupt")
        if expires > now:
            kept.append(row)
    return kept


def _fingerprint(kind: str, effect: str, scope: str, cost: str,
                 session_id: str) -> str:
    raw = "\x1f".join((kind, effect, scope, cost, session_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _trusted_generation(ctx: Any, responsibility_id: str) -> int | None:
    """Resolve a generation from the ledger, never from the model argument."""
    if not responsibility_id:
        return None
    try:
        from . import portal_responsibilities as responsibilities
        R = responsibilities._resp()
        record = R.inspect(responsibilities._store(), responsibility_id,
                           include_claim=False)
        value = record.get("generation")
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    except Exception as exc:  # noqa: BLE001 - an unverifiable record grants nothing
        raise ApprovalError("proposal responsibility could not be verified") from exc


def _onboarding_ref(ctx: Any) -> str:
    """A trusted snapshot of onboarding state; a later edit invalidates an ask."""
    facade = _facade(ctx)
    if facade is None:
        return ""
    try:
        raw = facade.get("onboarding_state")
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(raw, Mapping):
        return ""
    relevant = {key: raw.get(key) for key in (
        "version", "status", "updated_at", "completed_at", "settings", "grants",
        "destination", "applied")}
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _turn_rows(ctx: Any) -> list[dict[str, Any]]:
    facade = _facade(ctx)
    if facade is None:
        return []
    try:
        rows = facade.get("responsibility_human_turn")
    except Exception as exc:  # noqa: BLE001 - unreadable context denies
        raise ApprovalError("human turn state could not be read") from exc
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ApprovalError("human turn state is corrupt")
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _originating_turn(ctx: Any, session_id: Any = "") -> dict[str, Any]:
    now = time.time()
    rows = [row for row in _turn_rows(ctx)
            if float(row.get("at") or 0) >= now - 30 * 60
            and str(row.get("text") or "").strip()]
    requested = _clean(session_id, 200)
    if requested:
        rows = [row for row in rows
                if _clean(row.get("session_id"), 200) == requested]
    if not rows:
        raise ApprovalError(
            "an approval question requires a current foreground human conversation")
    sessions = {_clean(row.get("session_id"), 200) for row in rows}
    if not requested and len(sessions) != 1:
        raise ApprovalError(
            "the current conversation session is ambiguous; ask in that session")
    return max(rows, key=lambda row: float(row.get("at") or 0))


def _policy_ref(ctx: Any) -> str:
    """Canonical fingerprint of the currently validated permission context."""
    try:
        from proactive_pilot.policy import load_policy
        from proactive_pilot.userconfig import apply_user_config, load_user_config
        import paths as plugin_paths

        loaded = load_user_config(plugin_paths.user_config_path(), native=ctx)
        if not loaded.present:
            return ""
        policy = apply_user_config(load_policy(plugin_paths.policy_path()),
                                   loaded.config)
        config = loaded.config or {}
        relevant = {
            "sources": config.get("sources") or {},
            "preparation": config.get("preparation") or {},
            "destination": (config.get("notification") or {}).get("destination", ""),
            "preparation_enabled": policy.get("preparation_enabled"),
            "preparation_mode": policy.get("preparation_mode"),
            "preparation_scopes": policy.get("preparation_scopes"),
            "preparation_source_consent": policy.get("preparation_source_consent"),
        }
        raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 - unverifiable permission context
        return ""


def request(ctx: Any, *, kind: Any, effect: Any, scope: Any,
            cost: Any = "", ttl_minutes: Any = None,
            session_id: Any = "", responsibility_id: Any = "",
            generation: Any = None) -> dict[str, Any]:
    """Create or return ONE pending approval for the same exact proposal."""
    now = time.time()
    kind_text = str(kind or "external").strip().lower()
    if kind_text not in _KINDS:
        raise ApprovalError("unsupported proposal kind")
    effect_text = _clean(effect, 500)
    scope_text = _clean(scope, 240)
    cost_text = _clean(cost, 120)
    if not effect_text:
        raise ApprovalError("proposal effect is required")
    if not scope_text:
        raise ApprovalError("proposal scope is required")
    try:
        minutes = int(ttl_minutes) if ttl_minutes is not None else 30
    except (TypeError, ValueError) as exc:
        raise ApprovalError("ttl_minutes must be an integer") from exc
    minutes = max(1, min(minutes, 120))

    turn = _originating_turn(ctx, session_id)
    bound_session = _clean(turn.get("session_id"), 200)
    platform = _clean(turn.get("platform"), 40).lower()
    chat_type = _clean(turn.get("chat_type"), 40).lower()
    policy_ref = _policy_ref(ctx)
    if not policy_ref:
        raise ApprovalError("current permission context could not be verified")
    responsibility_key = _clean(responsibility_id, 96)
    trusted_generation = _trusted_generation(ctx, responsibility_key)
    onboarding_ref = _onboarding_ref(ctx)

    fingerprint = _fingerprint(kind_text, effect_text, scope_text, cost_text,
                               bound_session)
    with _STATE_LOCK:
        rows = _prune(_load(ctx), now)
        for row in rows:
            if row.get("fingerprint") == fingerprint:
                _save(ctx, rows)
                return {"ok": True, "deduplicated": True,
                        "approval": dict(row)}
        ref = "sec-apr-" + secrets.token_hex(6)
        row = {
            "schema": SCHEMA,
            "ref": ref,
            "fingerprint": fingerprint,
            "kind": kind_text,
            "effect": effect_text,
            "scope": scope_text,
            "cost": cost_text,
            "status": "pending",
            "created_at": now,
            "expires_at": now + minutes * 60,
            "approved_at": None,
            "evidence_quote": "",
            "session_id": bound_session,
            "platform": platform,
            "chat_type": chat_type,
            "policy_ref": policy_ref,
            "onboarding_ref": onboarding_ref,
            "responsibility_id": responsibility_key,
            "generation": trusted_generation,
        }
        rows.append(row)
        _save(ctx, rows)
    return {"ok": True, "deduplicated": False, "approval": dict(row),
            "note": ("One exact approval question. Nothing has been sent, "
                     "published, booked, purchased or changed.")}


def pending(ctx: Any) -> dict[str, Any]:
    with _STATE_LOCK:
        rows = _prune(_load(ctx), time.time())
        _save(ctx, rows)
    return {"ok": True, "approvals": [dict(row) for row in rows]}


def _approval_verdict(text: Any, reference: str) -> tuple[str, str]:
    """Return (approve|clarify|deny, reason). Conservative by construction."""
    raw = _clean(text, 2000)
    if not raw:
        return "deny", "the current human turn is empty"
    if _QUOTED.search(raw) or _REPORTED.search(raw):
        return "deny", "quoted or reported text is not an explicit approval"
    if _NEGATION.search(raw):
        return "deny", "the current human turn negates or cancels approval"
    if _CONDITIONAL.search(raw):
        return "deny", "a conditional or conflicting reply does not approve"
    if not _AFFIRMATIVE.search(raw):
        return "deny", "the current human turn is not an explicit affirmative reply"

    refs = {match.lower() for match in _REF.findall(raw)}
    if refs - {reference.lower()}:
        return "deny", "the reply names a different approval reference"
    if reference.lower() not in refs:
        return "clarify", ("which approval do you mean? Reply yes to the exact current "
                           "approval question and include its reference")

    stripped = _REF.sub(" ", raw).lower()
    words = re.findall(r"[a-z]+", stripped)
    if not words or any(word not in _ALLOWED_WORDS for word in words):
        return "deny", "the reply mixes approval with unrelated or conflicting content"
    return "approve", "exact current approval"


def approve(ctx: Any, *, reference: Any, evidence_quote: Any = "",
            session_id: Any, revoked: Any = None,
            reply_context: Any = "", policy_ref: Any = "") -> dict[str, Any]:
    """Consume exactly one current proposal after an explicit foreground yes.

    ``reply_context`` is accepted only for old callers and is never authority:
    the exact reference must appear in the real current human turn.
    """
    del reply_context
    now = time.time()
    ref = _clean(reference, 80).lower()
    if not _REF.fullmatch(ref):
        raise ApprovalError("an opaque approval reference is required")
    sid = _clean(session_id, 200)
    if not sid:
        raise ApprovalError("approval requires the current conversation session")
    if not callable(revoked):
        raise ApprovalError("approval requires authoritative current-scope validation")

    quote = _clean(evidence_quote, 2000)
    with _STATE_LOCK:
        rows = _prune(_load(ctx), now)
        target = next((row for row in rows
                       if str(row.get("ref") or "").lower() == ref), None)
        if target is None:
            _save(ctx, rows)
            raise ApprovalError("approval reference is unknown, stale or expired")
        if target.get("status") != "pending":  # defensive under the lock
            raise ApprovalError("approval was already consumed")

        bound_session = _clean(target.get("session_id"), 200)
        if not bound_session or bound_session != sid:
            raise ApprovalError("approval belongs to a different conversation session")

        turn = _originating_turn(ctx, sid)
        if _clean(turn.get("session_id"), 200) != sid:
            raise ApprovalError("no current human turn for this approval session")
        bound_platform = _clean(target.get("platform"), 40).lower()
        turn_platform = _clean(turn.get("platform"), 40).lower()
        if bound_platform and turn_platform and bound_platform != turn_platform:
            raise ApprovalError("approval belongs to a different channel")
        bound_chat = _clean(target.get("chat_type"), 40).lower()
        turn_chat = _clean(turn.get("chat_type"), 40).lower()
        if bound_chat and turn_chat and bound_chat != turn_chat:
            raise ApprovalError("approval belongs to a different chat type")

        turn_at = float(turn.get("at") or 0)
        created_at = float(target.get("created_at") or 0)
        expires_at = float(target.get("expires_at") or 0)
        if turn_at + 2 < created_at:
            raise ApprovalError("the approval turn predates this proposal; ask again")
        if turn_at > expires_at:
            raise ApprovalError("approval reference is expired")

        text = _clean(turn.get("text"), 2000)
        if quote and quote not in text:
            raise ApprovalError("evidence_quote is not present in the current human turn")

        verdict, reason = _approval_verdict(text, ref)
        if verdict == "clarify":
            raise ApprovalError(reason)
        if verdict != "approve":
            raise ApprovalError(reason)

        responsibility_key = _clean(target.get("responsibility_id"), 96)
        if responsibility_key:
            current_generation = _trusted_generation(ctx, responsibility_key)
            if current_generation is None:
                raise ApprovalError("proposal responsibility is no longer current")
            stored_generation = target.get("generation")
            if not isinstance(stored_generation, int) or stored_generation != current_generation:
                raise ApprovalError("the responsibility changed after this approval request")

        stored_onboarding = str(target.get("onboarding_ref") or "")
        if stored_onboarding and stored_onboarding != _onboarding_ref(ctx):
            raise ApprovalError("onboarding settings changed after this approval request")

        current_policy = _policy_ref(ctx)
        stored_policy = str(target.get("policy_ref") or "")
        if policy_ref and _clean(policy_ref, 80) != current_policy:
            raise ApprovalError("the supplied policy context is stale")
        if not stored_policy or not current_policy or stored_policy != current_policy:
            raise ApprovalError("permissions or source grants changed; ask again")

        try:
            revocation_reason = revoked(target)
        except Exception as exc:  # noqa: BLE001 - unknown revocation fails closed
            raise ApprovalError("approval context could not be verified") from exc
        if revocation_reason:
            target["status"] = "revoked"
            _save(ctx, rows)
            raise ApprovalError(str(revocation_reason))

        target["status"] = "approved"
        target["approved_at"] = now
        target["evidence_quote"] = quote or text
        _save(ctx, rows)
    return {"ok": True, "approved": dict(target),
            "grants": "this_proposal_only",
            "execution": "not_performed",
            "note": ("Only this exact proposal is approved. It performs no external "
                     "effect here and grants no standing permission.")}


def cancel(ctx: Any, *, reference: Any) -> dict[str, Any]:
    now = time.time()
    ref = _clean(reference, 80).lower()
    with _STATE_LOCK:
        rows = _prune(_load(ctx), now)
        target = next((row for row in rows
                       if str(row.get("ref") or "").lower() == ref), None)
        if target is None:
            _save(ctx, rows)
            raise ApprovalError("approval reference is unknown, stale or expired")
        target["status"] = "cancelled"
        _save(ctx, rows)
    return {"ok": True, "cancelled": dict(target)}


__all__ = ["APPROVALS_KEY", "ApprovalError", "request", "pending", "approve",
           "cancel"]
