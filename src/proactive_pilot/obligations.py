"""General obligation / transaction follow-through (read-only, evidence-bound).

Scope:
- Extracts action-oriented obligations (payment due/failed, orders needing
  action, renewals/cancellation deadlines, forms/RSVPs, appointments/deadlines,
  material opportunities) from the two authorised Himalaya accounts.
- Historical bootstrap is bounded to at most 90 days; ordinary mail remains the
  existing 48h window and is collected separately.
- Deterministic, conservative extraction only. A due date is recorded ONLY when
  an explicit, plausible date appears next to an action cue. Nothing is invented
  from ads or marketing, and every deterministic signal is labelled uncertain
  unless it carries an explicit reference/order identifier.
- Monotonic lifecycle: pending -> satisfied/cancelled (terminal). Silence, absence
  or an elapsed date NEVER closes an obligation. Resolution requires a matching
  resolution message sharing the same stable object key.
- Only compact evidence (refs/status/due/provenance) is persisted. Raw bodies and
  signed payment links are never stored.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .collectors import (
    ALLOWED_MAIL_ACCOUNTS,
    SourceResult,
    collect_himalaya_native,
    configured_mail_accounts,
    safe_source_error,
)

SCHEMA = 1
DEFAULT_BOOTSTRAP_DAYS = 90
DEFAULT_INCREMENTAL_HOURS = 72

# Actionable categories. Keywords are conservative explicit signals.
ACTIONABLE: dict[str, tuple[str, ...]] = {
    "payment_failed": (
        "payment failed", "payment declined", "card declined",
        "unsuccessful payment", "could not process payment",
        "failed payment", "billing problem", "payment was not successful",
    ),
    "payment_due": (
        "payment due", "amount due", "balance due", "past due",
        "overdue payment", "invoice due", "outstanding balance",
        "payment is due", "due on", "autopay", "pay by",
    ),
    "order_action": (
        "action required", "action needed", "confirm your order",
        "complete your order", "verify your order", "order requires",
        "shipping address", "update your payment", "order on hold",
        "pending order", "finish your order",
    ),
    "renewal": (
        "renew", "renewal", "expires", "expiring", "expiration",
        "will be charged", "renews on", "subscription ends",
    ),
    "cancellation": (
        "cancel by", "cancellation", "opt out by", "before it renews",
        "cancellation deadline", "cancel before",
    ),
    "form_rsvp": (
        "rsvp", "respond by", "register by", "registration",
        "complete the form", "submit by", "fill out", "form due",
        "questionnaire",
    ),
    "appointment": (
        "appointment", "booking", "reservation", "scheduled for",
        "your visit", "check-in",
    ),
    "opportunity": (
        "invitation", "application", "applications", "scholarship", "grant",
        "call for", "opportunity", "fellowship", "admission",
        "interview invitation", "nomination",
    ),
}

RESOLUTION: dict[str, tuple[str, ...]] = {
    "satisfied": (
        "payment received", "thank you for your payment", "payment successful",
        "successfully paid", "we received your payment", "order confirmed",
        "order shipped", "confirmed your order", "registration confirmed",
        "response received", "booking confirmed", "appointment confirmed",
        "submission received", "application received", "completed successfully",
    ),
    "cancelled": (
        "subscription cancelled", "subscription canceled",
        "canceled successfully", "cancelled successfully",
        "cancellation confirmed", "order cancelled", "order canceled",
        "booking cancelled", "booking canceled", "appointment cancelled",
        "appointment canceled", "your cancellation",
    ),
}

# A resolution phrase may ONLY close obligations in the categories it can
# genuinely resolve. An "order confirmed"/"order shipped" message never closes a
# payment-due obligation, and a cancellation never closes a shipped order.
RESOLUTION_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("payment received", "satisfied", ("payment_due", "payment_failed")),
    ("thank you for your payment", "satisfied", ("payment_due", "payment_failed")),
    ("payment successful", "satisfied", ("payment_due", "payment_failed")),
    ("successfully paid", "satisfied", ("payment_due", "payment_failed")),
    ("we received your payment", "satisfied", ("payment_due", "payment_failed")),
    ("order confirmed", "satisfied", ("order_action",)),
    ("order shipped", "satisfied", ("order_action",)),
    ("confirmed your order", "satisfied", ("order_action",)),
    ("registration confirmed", "satisfied", ("form_rsvp",)),
    ("response received", "satisfied", ("form_rsvp",)),
    ("submission received", "satisfied", ("form_rsvp", "opportunity")),
    ("application received", "satisfied", ("form_rsvp", "opportunity")),
    ("completed successfully", "satisfied", ("form_rsvp", "opportunity", "appointment")),
    ("booking confirmed", "satisfied", ("appointment",)),
    ("appointment confirmed", "satisfied", ("appointment",)),
    ("subscription cancelled", "cancelled", ("renewal", "cancellation")),
    ("subscription canceled", "cancelled", ("renewal", "cancellation")),
    ("canceled successfully", "cancelled", ("renewal", "cancellation")),
    ("cancelled successfully", "cancelled", ("renewal", "cancellation")),
    ("cancellation confirmed", "cancelled", ("renewal", "cancellation")),
    ("order cancelled", "cancelled", ("order_action",)),
    ("order canceled", "cancelled", ("order_action",)),
    ("booking cancelled", "cancelled", ("appointment",)),
    ("booking canceled", "cancelled", ("appointment",)),
    ("appointment cancelled", "cancelled", ("appointment",)),
    ("appointment canceled", "cancelled", ("appointment",)),
    ("your cancellation", "cancelled", ("renewal", "cancellation")),
)

# Ads / marketing: never create an obligation from these unless an explicit
# order/reference identifier is present.
MARKETING_MARKERS: tuple[str, ...] = (
    "unsubscribe", "% off", "discount", "sale ends", "promotion", "coupon",
    "deal", "limited time", "newsletter", "shop now", "free gift",
    "membership offer", "black friday", "flash sale", "exclusive offer",
)

CATEGORY_ACTION_CUE = {
    "payment_failed": ("payment", "pay", "billing", "charg"),
    "payment_due": ("due", "pay", "invoice", "balance", "amount"),
    "order_action": ("order", "confirm", "verify", "complete", "update"),
    "renewal": ("renew", "expire", "charg", "subscription", "plan"),
    "cancellation": ("cancel", "opt out", "before"),
    "form_rsvp": ("respond", "register", "submit", "form", "rsvp", "by"),
    "appointment": ("appointment", "booking", "appoint", "visit", "schedule"),
    "opportunity": ("apply", "application", "deadline", "submit", "invitation", "by"),
}

_REF_RE = re.compile(
    r"\b(?:order|invoice|reference|ref|booking|confirmation|case|ticket|"
    r"policy|account|subscription|application)"
    r"\s*(?:(number|no\.?|id|#)\s*)?[:#]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-]{3,})\b",
    re.IGNORECASE,
)
# Common words that can follow a keyword but are NOT identifiers.
_REF_WORD_DENY = frozenset({
    "confirmed", "confirm", "shipped", "cancelled", "canceled", "received",
    "successful", "successfully", "processed", "processing", "pending",
    "required", "action", "status", "details", "update", "number", "please",
    "your", "ready", "shipping", "delivery", "cancellation", "renewal",
})
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DMY_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_MDY_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.IGNORECASE,
)
_DUE_CUES = ("due", "by", "before", "expires", "expiring", "deadline",
             "renew", "renews", "cancel by", "pay by", "submit by", "respond by")


def _now(clock: Any) -> datetime:
    now = clock.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def sender_domain(sender: str) -> str:
    raw = str(sender or "").strip().lower()
    if "@" in raw:
        raw = raw.rsplit("@", 1)[-1]
    return raw.strip(">").strip()


def normalize_subject(subject: str) -> str:
    text = str(subject or "").lower()
    text = re.sub(r"^(re|fwd|fw)\s*:\s*", "", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())[:60]


def extract_reference(text: str) -> str:
    """Return a normalized explicit identifier, or ''.

    A bare word that merely follows a keyword (e.g. "order confirmed") is NOT an
    identifier. A value is accepted only when it contains a digit, or when it was
    introduced by an explicit marker (number/no/id/#).
    """
    for match in _REF_RE.finditer(str(text or "")):
        marker = (match.group(1) or "").strip()
        value = match.group(2) or ""
        lowered = value.lower()
        if lowered in _REF_WORD_DENY:
            continue
        if not any(ch.isdigit() for ch in value) and not marker:
            continue
        return re.sub(r"[^a-z0-9]", "", lowered)
    return ""


def object_key(*, account: str, ref_number: str, sender: str, subject: str, category: str = "") -> str:
    """Stable object/conversation key, scoped by account, sender and category.

    Explicit identifiers are scoped by the receiving account AND sender domain,
    so two different merchants' "order12345" never collide. The category is part
    of the key so an order's payment obligation and its shipping obligation are
    distinct objects. Resolution is linked only via this shared key; a merchant
    name plus a generic subject is never sufficient.
    """
    domain = sender_domain(sender)
    cat = str(category or "")
    if ref_number:
        digest = hashlib.sha256(
            f"{account}|{domain}|{ref_number}|{cat}".encode("utf-8")
        ).hexdigest()[:16]
        return f"obj:ref:{digest}"
    stem = normalize_subject(subject)
    digest = hashlib.sha256(f"{account}|{domain}|{stem}|{cat}".encode("utf-8")).hexdigest()[:16]
    return f"obj:thread:{digest}"


def extract_due_date(text: str, now: datetime) -> tuple[str, bool]:
    """Return ``(iso_date, explicit)``. Empty date means no explicit due date.

    A date is accepted only when an action/due cue appears within a short window
    (<=80 chars) of the exact date match. A generic date elsewhere in the body is
    never promoted to a deadline. Past dates are kept (an overdue obligation must
    remain overdue) and are never treated as satisfaction.
    """
    lowered = str(text or "").lower()
    if not any(cue in lowered for cue in _DUE_CUES):
        return "", False
    for pattern in (_ISO_DATE_RE, _DMY_RE, _MDY_RE):
        for match in pattern.finditer(lowered):
            start, end = match.start(), match.end()
            window = lowered[max(0, start - 80): min(len(lowered), end + 80)]
            if not any(cue in window for cue in _DUE_CUES):
                continue
            groups = match.groups()
            try:
                if pattern is _ISO_DATE_RE:
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                elif pattern is _DMY_RE:
                    day, month = int(groups[0]), _MONTHS[groups[1].lower()]
                    year = int(groups[2])
                else:
                    month = _MONTHS[groups[0].lower()]
                    day, year = int(groups[1]), int(groups[2])
                candidate = datetime(year, month, day)
            except (ValueError, KeyError):
                continue
            if not (2000 <= year <= 2100):
                continue
            return candidate.date().isoformat(), True
    return "", False


def _category_for(text: str) -> str:
    for category, phrases in ACTIONABLE.items():
        if any(phrase in text for phrase in phrases):
            return category
    return ""


def _action_cue_present(text: str, category: str) -> bool:
    cues = CATEGORY_ACTION_CUE.get(category, ())
    return any(cue in text for cue in cues)


def _is_marketing(text: str) -> bool:
    return any(marker in text for marker in MARKETING_MARKERS)


def extract_signals(
    *,
    account: str,
    source_ref: str,
    sender: str,
    subject: str,
    preview: str,
    date: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Extract conservative obligation signals from one message's redacted text."""
    text = f"{subject} {preview}".lower()
    if not text.strip():
        return []
    ref_number = extract_reference(f"{subject} {preview}")
    marketing = _is_marketing(text)

    matched_status = ""
    resolves: list[str] = []
    for phrase, status, categories in RESOLUTION_RULES:
        if phrase in text:
            matched_status = status
            for category in categories:
                if category not in resolves:
                    resolves.append(category)
    if matched_status:
        return [{
            "kind": "resolution",
            "status": matched_status,
            "category": "",
            "resolves_categories": resolves,
            "object_key": "",
            "due_date": "",
            "confidence": "explicit",
            "ref_number": ref_number,
            "source_ref": source_ref,
            "account": account,
            "sender": sender,
            "subject": subject[:200],
            "when": date,
        }]

    category = _category_for(text)
    if not category:
        return []
    if marketing and not ref_number:
        return []  # never invent an obligation from an ad
    if not _action_cue_present(text, category):
        return []
    due_date, explicit = extract_due_date(text, now)
    confidence = "explicit" if (ref_number and explicit) else "uncertain"
    return [{
        "kind": "obligation",
        "status": "pending",
        "category": category,
        "object_key": object_key(account=account, ref_number=ref_number,
                                 sender=sender, subject=subject, category=category),
        "due_date": due_date if explicit else "",
        "confidence": confidence,
        "ref_number": ref_number,
        "source_ref": source_ref,
        "account": account,
        "sender": sender,
        "subject": subject[:200],
        "when": date,
    }]


def new_store() -> dict[str, Any]:
    return {"schema": SCHEMA, "updated_at": "", "last_scan_at": "", "records": {},
            "cursors": {}}


def load_obligations(store: Any | None) -> dict[str, Any]:
    if store is None:
        return new_store()
    data = store.load("obligations.json", new_store())
    if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
        return new_store()
    return data


def save_obligations(store: Any | None, data: dict[str, Any]) -> None:
    if store is None:
        return
    store.save("obligations.json", data)


def merge_signals(records: dict[str, Any], signals: list[dict[str, Any]], now: datetime) -> None:
    """Apply signals under a monotonic lifecycle.

    New action signals create/refresh a pending record. Resolution signals only
    close a record that already exists AND shares the same stable object key.
    A closed record is never reopened by an identical/stale signal.
    """
    for sig in signals:
        if sig.get("kind") == "resolution":
            # Resolution may only close a live obligation whose category is one
            # the resolution phrase genuinely resolves, and only when the same
            # account/sender/identifier object key matches. Silence, absence and
            # elapsed dates never close anything.
            resolves = list(sig.get("resolves_categories") or [])
            ref_number = str(sig.get("ref_number") or "")
            account = str(sig.get("account") or "")
            sender = str(sig.get("sender") or "")
            subject = str(sig.get("subject") or "")
            for category in resolves:
                key = object_key(account=account, ref_number=ref_number,
                                 sender=sender, subject=subject, category=category)
                rec = records.get(key)
                if rec is None or rec.get("status") in {"satisfied", "cancelled"}:
                    continue
                rec["status"] = sig.get("status", "satisfied")
                rec["resolved_at"] = now.isoformat()
                rec["resolved_by"] = str(sig.get("source_ref") or "")
                rec.setdefault("evidence", [])
                if sig.get("source_ref") and sig["source_ref"] not in rec["evidence"]:
                    rec["evidence"].append(sig["source_ref"])
            continue

        key = str(sig.get("object_key") or "")
        if not key:
            continue
        rec = records.get(key)

        if rec is None:
            records[key] = {
                "object_key": key,
                "status": "pending",
                "category": str(sig.get("category") or ""),
                "title": str(sig.get("subject") or "")[:200],
                "due_date": str(sig.get("due_date") or ""),
                "confidence": str(sig.get("confidence") or "uncertain"),
                "ref_number": str(sig.get("ref_number") or ""),
                "account": str(sig.get("account") or ""),
                "sender": str(sig.get("sender") or ""),
                "first_seen": now.isoformat(),
                "last_seen": now.isoformat(),
                # Actual source-event time (message date), not collection time.
                "source_time": str(sig.get("when") or ""),
                "source_refs": [str(sig.get("source_ref") or "")],
                "evidence": [str(sig.get("source_ref") or "")],
                "resolved_at": "",
                "resolved_by": "",
            }
            continue
        if rec.get("status") in {"satisfied", "cancelled"}:
            continue  # monotonic: never reopen
        rec["last_seen"] = now.isoformat()
        if sig.get("category"):
            rec["category"] = str(sig["category"])
        # Only set a due date when it is explicit; never overwrite a known
        # explicit date with an empty one (and never invent one).
        if sig.get("due_date"):
            rec["due_date"] = str(sig["due_date"])
        if sig.get("confidence") == "explicit":
            rec["confidence"] = "explicit"
        refs = rec.setdefault("source_refs", [])
        src = str(sig.get("source_ref") or "")
        if src and src not in refs:
            refs.append(src)
        if sig.get("subject"):
            rec["title"] = str(sig["subject"])[:200]


def prune(records: dict[str, Any], now: datetime, *, retention_days: int) -> None:
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    for key in list(records):
        rec = records[key]
        if rec.get("status") not in {"satisfied", "cancelled"}:
            continue
        try:
            resolved = datetime.fromisoformat(str(rec.get("resolved_at") or ""))
        except ValueError:
            continue
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=timezone.utc)
        if resolved < cutoff:
            records.pop(key, None)


def _item_from_record(account: str, rec: dict[str, Any], now: datetime) -> dict[str, Any]:
    key = str(rec.get("object_key") or "")
    status = str(rec.get("status") or "pending")
    due = str(rec.get("due_date") or "")
    first_seen = str(rec.get("first_seen") or "")
    # Age is measured from the actual source-event time where available, so a
    # 90-day historical bootstrap is flagged historical instead of looking fresh.
    source_time = str(rec.get("source_time") or first_seen)
    age_days = 0
    try:
        parsed = datetime.fromisoformat(source_time)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_days = max(0, int((now - parsed).days))
    except ValueError:
        age_days = 0
    # Stable content identity: lifecycle status/due/category/evidence only.
    # Never includes last_seen/collection time, so an unchanged unresolved
    # obligation cannot re-wake the evaluator on every run.
    evidence = sorted(str(x) for x in (rec.get("evidence") or []) if x)
    content_version = hashlib.sha256(
        "|".join([status, due, str(rec.get("category") or ""), ",".join(evidence)]).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "ref": f"obligation:{account}:{key}",
        "id": key,
        "title": str(rec.get("title") or "(obligation)"),
        "subject": str(rec.get("title") or ""),
        "category": str(rec.get("category") or ""),
        "status": status,
        "due_date": due,
        # Date-only obligation deadlines are never a concrete clock time, so
        # they can never substantiate urgency on their own.
        "all_day": bool(due and len(due) == 10),
        "confidence": str(rec.get("confidence") or "uncertain"),
        "content_version": content_version,
        "source_time": source_time,
        "age_days": age_days,
        # Observed age/confidence labels keep a historical bootstrap low-noise.
        "historical": age_days > 7,
        "account": account,
        # Deliberately NOT last_seen: avoids per-run evidence-version churn.
        "updated": "",
        "provenance": str(rec.get("sender") or ""),
        "source": account,
    }


def collect_obligations(
    policy: dict[str, Any],
    clock: Any,
    *,
    store: Any | None = None,
    runner: Any | None = None,
    now: datetime | None = None,
    budget: Any | None = None,
    pre_commit: Callable[[list[SourceResult]], None] | None = None,
) -> list[SourceResult]:
    """Collect and fold obligation signals across both authorised accounts.

    Historical follow-through uses the SUPPORTED protocol-native Gmail metadata
    route (``gmail messages list``/``get``), NOT the shared ``envelope search``
    command that the Gmail backend rejects at runtime. The 90-day bootstrap is
    driven by a persisted STABLE window cutoff plus a resumable cursor (opaque
    provider page token + within-page offset), reads at most
    ``obligation_metadata_items_per_run`` metadata gets per account per run, and
    is declared partial until the window is genuinely exhausted. Only minimum
    extracted fields are persisted; raw headers/bodies/snippets never are.
    """
    if not bool(policy.get("obligations_enabled", True)):
        return []
    now = now or _now(clock)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    data = load_obligations(store)
    data.setdefault("cursors", {})
    max_days = int(policy.get("obligation_bootstrap_days", DEFAULT_BOOTSTRAP_DAYS))
    window_hours = max_days * 24
    # Native id pages stay small (default 10, caller-lowered source_page_size
    # still authoritative) so one bounded page fits the obligation window.
    page_size = max(1, min(int(policy.get("source_page_size", 50)),
                           int(policy.get("obligation_page_size", 10))))
    limit = int(policy.get("obligation_scan_limit", 300))
    preview_limit = int(policy.get("obligation_preview_items", 3))
    preview_chars = int(policy.get("obligation_preview_chars", 200))
    preview_timeout = int(policy.get("obligation_preview_timeout_seconds", 10))
    # At most a few metadata gets per account per run, so the historical read is
    # bounded regardless of mailbox size.
    max_metadata = max(1, int(policy.get("obligation_metadata_items_per_run", 3)))
    max_pages = max(1, int(policy.get("obligation_max_pages_per_run", 2)))

    # Persist a STABLE UTC window cutoff at bootstrap. Once set it is reused
    # verbatim on every later run so newly arriving mail can never shift the
    # numeric page/token cursor and cause unvisited historical ids to be skipped.
    cutoff_date = str(data.get("window_cutoff") or "")
    if not cutoff_date:
        cutoff_date = (now.date() - timedelta(days=max_days)).isoformat()
        data["window_cutoff"] = cutoff_date


    def _explicit_action(item: dict[str, Any]) -> bool:
        # Only explicit action/resolution subjects may consume a body preview:
        # obligations require content, routine browsing does not.
        text = str(item.get("subject") or "").lower()
        if _category_for(text) or any(any(p in text for p in phrases) for phrases in RESOLUTION.values()):
            return True
        return bool(_REF_RE.search(text))

    # Rotate which account starts, so a slow core pass can never indefinitely
    # starve the second obligation account. The cursor is persisted and flips
    # each run, but at least one account is always attempted.
    # Explicit configured access wins; ABSENT config keeps the legacy accounts.
    accounts = list(configured_mail_accounts(policy))
    account_cursor = int(data.get("account_cursor", 0)) % max(1, len(accounts))
    ordered_accounts = accounts[account_cursor:] + accounts[:account_cursor]
    data["account_cursor"] = (account_cursor + 1) % max(1, len(accounts))

    results: list[SourceResult] = []
    attempted = 0
    total_accounts = max(1, len(ordered_accounts))
    for idx, account in enumerate(ordered_accounts):
        prior = data["cursors"].get(account) or {}
        # Always keep a persisted cursor record so a deferred/timed-out attempt
        # still leaves meaningful partial-bootstrap state behind. The narrow
        # page-id QUEUE plus the opaque provider token and first-unvisited offset
        # make the native scan resumable without ever re-listing a page or
        # skipping unvisited ids when new mail shifts provider ordering.
        cursor = {
            "token": str(prior.get("token") or ""),
            "offset": max(0, int(prior.get("offset", 0) or 0)),
            "pending_ids": [str(x) for x in (prior.get("pending_ids") or []) if str(x)],
            "bootstrap_done": bool(prior.get("bootstrap_done")),
            "previewed_ids": list(prior.get("previewed_ids") or []),
            "scanned_total": int(prior.get("scanned_total", 0)),
            "ids_listed_total": int(prior.get("ids_listed_total", 0)),
            "last_error": str(prior.get("last_error") or ""),
            # Independent persisted INCREMENTAL window/token/queue state. It is
            # never reset mid-window on a partial run, so a steady partial scan
            # cannot forever re-read the first few newest ids.
            "inc_cutoff": str(prior.get("inc_cutoff") or ""),
            "inc_token": str(prior.get("inc_token") or ""),
            "inc_offset": max(0, int(prior.get("inc_offset", 0) or 0)),
            "inc_pending_ids": [str(x) for x in (prior.get("inc_pending_ids") or []) if str(x)],
            "inc_done": bool(prior.get("inc_done")),
        }
        data["cursors"][account] = cursor
        bootstrap_done = cursor["bootstrap_done"]
        # Incremental reads after a completed bootstrap use an independent,
        # persisted recent window. A new incremental window is started ONLY when
        # the previous one was genuinely exhausted (``inc_done``); a partial run
        # keeps its queue/token/offset so it makes steady progress instead of
        # re-scanning the first ids forever.
        if bootstrap_done:
            if cursor["inc_done"] or not cursor["inc_cutoff"]:
                cursor["inc_cutoff"] = (now.date() - timedelta(
                    hours=int(policy.get("obligation_incremental_hours",
                                         DEFAULT_INCREMENTAL_HOURS))
                )).isoformat()
                cursor["inc_token"] = ""
                cursor["inc_offset"] = 0
                cursor["inc_pending_ids"] = []
                cursor["inc_done"] = False
            account_cutoff = cursor["inc_cutoff"]
            start_token = cursor["inc_token"]
            start_offset = cursor["inc_offset"]
            start_pending = cursor["inc_pending_ids"]
        else:
            account_cutoff = cutoff_date
            start_token = cursor["token"]
            start_offset = cursor["offset"]
            start_pending = cursor["pending_ids"]
        if budget is not None and budget.expired():
            results.append(SourceResult(f"obligation:{account}", "ok", count=0, items=[],
                                        checked_at="", truncated=True,
                                        failure="obligation budget-deferred",
                                        next_page=(1 if bootstrap_done else 0),
                                        next_token=start_token, offset=start_offset,
                                        pending_ids=list(start_pending)))
            continue
        if budget is None:
            timeout = 20
        else:
            remaining = budget.remaining()
            # Fair per-account share of the reserved obligation slot: the first
            # account cannot consume the second account's window. The final
            # account gets whatever is left (clamped to the overall parent).
            share = remaining if idx >= total_accounts - 1 else remaining / (total_accounts - idx)
            timeout = int(min(20.0, share, remaining))
            if timeout < 1:
                results.append(SourceResult(f"obligation:{account}", "ok", count=0, items=[],
                                            checked_at="", truncated=True,
                                            failure="obligation budget-deferred",
                                            next_page=(1 if bootstrap_done else 0),
                                            next_token=start_token, offset=start_offset,
                                            pending_ids=list(start_pending)))
                continue
        # Never re-preview an id whose body was already read; this keeps the
        # dedicated preview budget for genuinely new actionable mail.
        skip_ids = set(str(x) for x in (cursor.get("previewed_ids") or []))
        dispatch = policy.get("_source_dispatch")
        if dispatch is not None and not dispatch(f"obligation:{account}"):
            # Preserve known obligations even while the provider circuit waits.
            active = [_item_from_record(account, rec, now) for rec in data["records"].values()
                      if str(rec.get("account") or "") == account
                      and rec.get("status") in {"pending", "uncertain"}]
            results.append(SourceResult(f"obligation:{account}", "stale", count=len(active),
                                        items=active, attempted=False,
                                        failure="source-health cooldown", truncated=True))
            continue
        try:
            meta = collect_himalaya_native(
                account, limit, clock=clock, cutoff_date=account_cutoff,
                page_size=page_size, runner=runner, preview_limit=preview_limit,
                preview_chars=preview_chars, preview_selector=_explicit_action,
                preview_timeout=preview_timeout, max_metadata=max_metadata,
                start_token=start_token, start_offset=start_offset,
                pending_ids=start_pending,
                timeout=timeout, preview_skip_ids=skip_ids,
                allowed_accounts=accounts,
            )
        except Exception as exc:  # noqa: BLE001 - surface as coverage failure
            cursor["last_error"] = safe_source_error(exc, kind="obligation")
            observe = policy.get("_source_observe")
            if observe is not None:
                observe(SourceResult(f"obligation:{account}", "error", error=cursor["last_error"],
                                     checked_at=now.isoformat(), attempted=True))
            results.append(SourceResult(f"obligation:{account}", "error",
                                        error=cursor["last_error"],
                                        checked_at="", truncated=True,
                                        next_token=start_token, offset=start_offset,
                                        pending_ids=list(start_pending)))
            continue
        attempted += 1
        observe = policy.get("_source_observe")
        if observe is not None:
            from dataclasses import replace
            observe(replace(meta, name=f"obligation:{account}"))
        meta_scanned = int(getattr(meta, "scanned", 0) or 0)
        meta_listed = int(getattr(meta, "ids_listed", 0) or 0)
        cursor["scanned_total"] = int(cursor.get("scanned_total", 0)) + meta_scanned
        cursor["ids_listed_total"] = int(cursor.get("ids_listed_total", 0)) + meta_listed
        # Surface the account's still-unresolved obligations even when the
        # source is partial/unavailable: they survive in state (never closed by
        # absence) and are reported under a stale status so they cannot present
        # as a fresh/urgent check.
        active = [
            _item_from_record(account, rec, now)
            for rec in data["records"].values()
            if str(rec.get("account") or "") == account
            and rec.get("status") in {"pending", "uncertain"}
        ]
        active.sort(key=lambda it: (str(it.get("due_date") or ""), str(it.get("ref") or "")))
        if not meta.healthy:
            # Keep retrying the same cursor next run, but persist the cursor and
            # the safe scanned count so partial coverage is meaningful and an
            # empty synthetic result is never mistaken for a failed scan.
            label = safe_source_error(meta.error or "unavailable", kind="obligation")
            cursor["last_error"] = label
            results.append(SourceResult(f"obligation:{account}", "stale",
                                        count=len(active), items=active,
                                        error=label, checked_at=meta.checked_at,
                                        failure=label,
                                        truncated=True, next_token=start_token,
                                        offset=start_offset,
                                        pending_ids=list(start_pending),
                                        pages_read=int(meta.pages_read or 0),
                                        scanned=meta_scanned, ids_listed=meta_listed))
            continue

        # Persist the resume cursor. Bootstrap completes only when the whole
        # 90-day window was covered in THIS pass from the start cursor. When the
        # budget (not the window) stopped the scan, advance from the ids actually
        # read: never jump past unread stable ids.
        previewed = list(cursor.get("previewed_ids") or [])
        for mid in (meta.previewed_ids or []):
            if mid not in previewed:
                previewed.append(mid)
        previewed = previewed[-200:]
        cursor["previewed_ids"] = previewed
        cursor["last_error"] = ""
        meta_pending = [str(x) for x in (getattr(meta, "pending_ids", []) or []) if str(x)]
        meta_token = str(getattr(meta, "next_token", "") or "")
        meta_offset = int(getattr(meta, "offset", 0) or 0)
        if bootstrap_done:
            # Independent incremental cursor: completion marks only an actually
            # exhausted recent window, never a partial run.
            if meta.window_complete:
                cursor["inc_done"] = True
                cursor["inc_pending_ids"] = []
                cursor["inc_token"] = ""
                cursor["inc_offset"] = 0
            else:
                cursor["inc_done"] = False
                cursor["inc_pending_ids"] = meta_pending
                cursor["inc_token"] = meta_token
                cursor["inc_offset"] = meta_offset
        else:
            if meta.window_complete:
                cursor["bootstrap_done"] = True
                cursor["pending_ids"] = []
                cursor["token"] = ""
                cursor["offset"] = 0
            else:
                cursor["bootstrap_done"] = False
                cursor["pending_ids"] = meta_pending
                cursor["token"] = meta_token
                cursor["offset"] = meta_offset

        signals: list[dict[str, Any]] = []
        for item in meta.items:
            signals.extend(extract_signals(
                account=account,
                source_ref=str(item.get("ref") or ""),
                sender=str(item.get("sender") or ""),
                subject=str(item.get("subject") or ""),
                preview=str(item.get("preview") or ""),
                date=str(item.get("date") or ""),
                now=now,
            ))
        merge_signals(data["records"], signals, now)
        active = [
            _item_from_record(account, rec, now)
            for rec in data["records"].values()
            if str(rec.get("account") or "") == account
            and rec.get("status") in {"pending", "uncertain"}
        ]
        active.sort(key=lambda it: (str(it.get("due_date") or ""), str(it.get("ref") or "")))
        results.append(SourceResult(
            f"obligation:{account}", "ok", count=len(active), items=active,
            checked_at=meta.checked_at, truncated=meta.truncated,
            window_complete=meta.window_complete, pages_read=meta.pages_read,
            next_token=meta_token, offset=meta_offset, pending_ids=list(meta_pending),
            scanned=meta_scanned, ids_listed=meta_listed,
        ))

    if pre_commit is not None:
        pre_commit(list(results))
    prune(data["records"], now, retention_days=int(policy.get("obligation_retention_days", 30)))
    data["last_scan_at"] = now.isoformat()
    data["updated_at"] = now.isoformat()
    save_obligations(store, data)
    return results


def health(data: dict[str, Any] | None) -> dict[str, Any]:
    """Safe counts only (no bodies, no links, no credentials)."""
    if not isinstance(data, dict):
        data = {}
    records = data.get("records", {})
    counts = {"pending": 0, "satisfied": 0, "cancelled": 0, "uncertain": 0}
    for rec in records.values():
        status = str(rec.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    # Safe, non-private scan progress: proves whether historical metadata was
    # actually examined and how far the resumable cursor advanced, without ever
    # exposing subjects, senders or bodies.
    cursors = data.get("cursors", {}) if isinstance(data.get("cursors"), dict) else {}
    scanned_total = 0
    listed_total = 0
    bootstrap_done = 0
    for cur in cursors.values():
        if isinstance(cur, dict):
            scanned_total += int(cur.get("scanned_total", 0) or 0)
            listed_total += int(cur.get("ids_listed_total", 0) or 0)
            if cur.get("bootstrap_done"):
                bootstrap_done += 1
    return {
        "total": len(records), **counts,
        "accounts_with_cursor": len(cursors),
        "bootstrap_done_accounts": bootstrap_done,
        # Actual ASSESSED metadata gets (successful, projectable); listed ids are
        # reported separately so "listed" is never conflated with "assessed".
        "metadata_scanned_total": scanned_total,
        "ids_listed_total": listed_total,
        # Stable bootstrap window cutoff (a plain UTC date, never private data):
        # proves the numeric/token cursor cannot shift as new mail arrives.
        "window_cutoff": str(data.get("window_cutoff") or ""),
    }
