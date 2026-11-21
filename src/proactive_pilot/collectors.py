"""Read-only, bounded collectors over already-authorized local sources.

Hard rules:
- Only fixed argv lists are executed; ``shell=False`` is always used. No model
  or user string is ever interpolated into a command.
- Calendar/Tasks use the shipped productivity-secretary library (unmodified):
  its authorized transport/credentials, with bounded ``nextPageToken`` paging.
- School mail is read through the existing Himalaya Gmail accounts only
  (``formal``/``informal``). No Graph / Power Automate path exists here. The
  shared ``envelope search`` command is advertised by the installed CLI but the
  Gmail backend rejects it at runtime, so the obligation history uses the
  supported protocol-native ``gmail messages list``/``get`` metadata route
  (bounded ids + opaque page token + a few metadata headers only).
- A page/account cap that stops before the declared window is covered is
  reported as ``truncated`` -> partial coverage, never healthy-complete.
- Mail bodies are read with a fixed read-only ``message read`` argv that never
  passes ``--seen`` (seen state is untouched). Only a short, redacted preview is
  retained; credential/OTP messages are never fetched and raw bodies are never
  written anywhere.
- Source/library locations follow the host's context-local active profile. The
  documented standalone HERMES_HOME/``~/.hermes`` fallback is used only when the
  host profile module is genuinely absent; an explicit operator override remains
  explicit.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlencode

from . import ownerpaths as _ownerpaths


class PortalLibraryUnavailable(RuntimeError):
    """The shipped productivity-secretary library could not be located.

    Raised (or surfaced as a failed ``SourceResult``) instead of leaking a bare
    ``ImportError`` so calendar/tasks callers fail closed with one actionable
    message. Public-source monitoring is unaffected and keeps working.
    """


SECRETARY_SRC_ENV = "SECRETARY_SRC"
SECRETARY_SRC_ENV_ALT = "PRODUCTIVITY_SECRETARY_SRC"


def secretary_src_candidates() -> list[str]:
    """Ordered candidate locations for the productivity-secretary ``src`` dir.

    Order: explicit operator env override, then the active profile's
    ``productivity-secretary/src``, then ``~/productivity-secretary/src``. An
    explicit override is never rewritten to a profile path. A present host
    profile that cannot be resolved fails closed instead of reading the launch
    profile's home.
    """
    candidates: list[str] = []
    override = os.environ.get(SECRETARY_SRC_ENV) or os.environ.get(SECRETARY_SRC_ENV_ALT)
    if override:
        candidates.append(str(override))
    base = _ownerpaths.hermes_home()
    candidates.append(str(base / "productivity-secretary" / "src"))
    candidates.append(str(Path.home() / "productivity-secretary" / "src"))
    return candidates


def resolve_secretary_src() -> str:
    """Return the first candidate that exists, else the first candidate.

    The fallback is only used to name a missing location in diagnostics; callers
    must still check :func:`secretary_library_error` before importing.
    """
    candidates = secretary_src_candidates()
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return candidate
    return candidates[0] if candidates else ""


def _default_secretary_src() -> str:
    """Profile-safe path to the shipped productivity-secretary library."""
    return resolve_secretary_src()


SECRETARY_SRC = _default_secretary_src()


def secretary_library_error() -> str:
    """Actionable fail-closed message, or '' when the library is present.

    One clear message naming the missing library and the env override so a
    caller can never mistake the absence for "nothing to report".
    """
    if Path(resolve_secretary_src()).is_dir():
        return ""
    searched = ", ".join(c for c in secretary_src_candidates() if c)
    return (
        "productivity-secretary library not found; calendar/tasks monitoring is "
        f"disabled. Set {SECRETARY_SRC_ENV} (or {SECRETARY_SRC_ENV_ALT}) to its "
        f"'src' directory to enable it. Public-source monitoring continues without "
        f"it. Searched: {searched}"
    )


def _insert_secretary_src() -> str:
    """Put the resolved library on ``sys.path`` or fail closed with a message."""
    src = resolve_secretary_src()
    if not Path(src).is_dir():
        raise PortalLibraryUnavailable(secretary_library_error())
    if src not in sys.path:
        sys.path.insert(0, src)
    return src


ALLOWED_MAIL_ACCOUNTS: tuple[str, ...] = ("formal", "informal")


def configured_mail_accounts(policy: dict[str, Any] | None) -> tuple[str, ...]:
    """Explicit mail account allowlist for this run.

    When the validated user configuration supplies ``mail_accounts`` (mapped to
    ``policy['mail_accounts']``), only those explicitly configured names are
    read; an empty list means "no private mail". When the key is ABSENT the
    legacy implicit behaviour (``formal``/``informal``) is preserved exactly, so
    existing deployments and tests are unchanged.
    """
    if isinstance(policy, dict) and "mail_accounts" in policy:
        raw = policy.get("mail_accounts")
        if isinstance(raw, (list, tuple)):
            out: list[str] = []
            for item in raw:
                name = str(item or "").strip()
                if name and name not in out:
                    out.append(name)
            return tuple(out)
    return ALLOWED_MAIL_ACCOUNTS


def configured_calendar_accounts(policy: dict[str, Any] | None) -> list[str] | None:
    """Configured calendar accounts, or ``None`` to keep library discovery.

    ``None`` (key absent) preserves the legacy behaviour of using every alias
    the compatibility library reports. An explicit list (even empty) narrows
    reads to exactly the configured accounts -- credentials alone never widen
    access.
    """
    if isinstance(policy, dict) and "calendar_accounts" in policy:
        raw = policy.get("calendar_accounts")
        if isinstance(raw, (list, tuple)):
            return [str(item).strip() for item in raw if str(item or "").strip()]
        return []
    return None


def _task_aliases(policy: dict[str, Any]) -> list[str]:
    """Independent task scope; shared aliases only for legacy omitted scope."""
    if "task_accounts" in policy:
        raw = policy["task_accounts"]
        if not isinstance(raw, (list, tuple)):
            return []
        return list(dict.fromkeys(item.strip() for item in raw
                                  if isinstance(item, str) and item.strip()))
    return _google_aliases(policy)


def source_enabled(policy: dict[str, Any], key: str) -> bool:
    """Per-source enablement, defaulting ON to preserve legacy behaviour.

    A validated user configuration always sets these keys; without one the
    absent key means "on" exactly as before.
    """
    if isinstance(policy, dict) and key in policy:
        return bool(policy.get(key))
    return True


DEFAULT_PAGE_SIZE = 50
MAX_PAGES = 20
MAX_STDOUT = 512 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Credential/OTP subjects are never read and are redacted wherever they appear.
_SENSITIVE_TERMS = (
    "otp", "one-time", "one time", "verification code", "verify your",
    "passcode", "password", "security code", "2fa", "two-factor",
    "two factor", "sign-in code", "sign in code", "login code",
    "magic link", "confirmation code", "authentication code", "credential",
)
_OTP_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


@dataclass
class SourceResult:
    name: str
    status: str  # ok | error | stale
    count: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    checked_at: str = ""
    # True when the declared window/list set could not be fully covered within
    # the item/page caps. A truncated read must never be reported as complete.
    truncated: bool = False
    # Public feeds may serve cached evidence when cadence/rate limits apply or a
    # fetch fails. ``cache_at`` is the true observation time of the cached body,
    # so stale cache is never treated as fresh (and never urgent).
    cached: bool = False
    cache_at: str = ""
    cache_age_minutes: int = 0
    failure: str = ""
    # Resumable pagination cursor for large historical windows (obligations).
    next_page: int = 0
    window_complete: bool = False
    pages_read: int = 0
    # Count of metadata rows actually ASSESSED this call (successful metadata
    # gets that produced a projected item), never the number of ids merely
    # listed. Safe count only, never subjects/senders. Lets acceptance prove a
    # real bounded scan even when zero obligations were extracted, instead of
    # inferring failure from an empty synthetic result.
    scanned: int = 0
    # Raw number of provider ids listed this call (safe integer only). Kept
    # separate from ``scanned`` so "listed 10, assessed 3" is never conflated
    # with "assessed 10".
    ids_listed: int = 0
    # Message ids whose body was actually previewed this run (so a dedicated
    # obligation preview is not repeated for a known stable id next run).
    previewed_ids: list[str] = field(default_factory=list)
    # Resumable OPAQUE provider page cursor (e.g. Gmail's ``next_page`` token).
    # A non-empty token means the provider has more ids beyond those already
    # consumed; it is persisted and replayed verbatim, never parsed or synthesized.
    next_token: str = ""
    # Within-page offset into the already-fetched id list, so a run that is
    # interrupted mid-page does not skip the unvisited ids of that page.
    offset: int = 0
    # Persisted QUEUE of the current provider page's message ids (narrow ids
    # only, never raw metadata). Reusing this queue means a run that resumes
    # mid-page never re-lists the page, so provider ordering changes and new
    # arrivals at the head cannot shift numeric offsets and skip unvisited
    # historical ids. ``offset`` is the first unvisited index into this queue.
    pending_ids: list[str] = field(default_factory=list)
    # False only for an explicitly skipped dispatch. None preserves legacy
    # attempted-result contracts; wrappers must not fabricate fresh evidence.
    attempted: bool | None = None

    @property
    def healthy(self) -> bool:
        return self.status == "ok"


class SourceTimeout(Exception):
    """An approved source call exceeded its wall-clock bound.

    This is a *budget* event, never an authentication/authorization failure: the
    caller must preserve whatever evidence was collected and mark the source
    partial/budget-deferred.
    """

    def __init__(self, message: str = "source call exceeded wall-clock bound") -> None:
        super().__init__(message)


@contextlib.contextmanager
def wallclock_alarm(seconds: float):
    """Bound one approved source call using a local, restorable SIGALRM.

    The shipped secretary library's native GET transport does not thread the
    caller's timeout through, so a single call can hang past the collector
    budget. The per-source bound is registered with the shared watchdog so that
    it composes with an enclosing whole-run ``run_deadline``: the earliest
    deadline always wins, nesting can never postpone the total, and a longer
    inner bound can never mask an earlier outer one. This function keeps its own
    exception identity (``SourceTimeout``) so collectors can still degrade to
    budget-deferred once the per-source deadline -- and only that one -- fires.

    Platform assumption: meaningful only on Linux/Unix with SIGALRM and entered
    from the MAIN thread. Off the main thread, or without SIGALRM, it is a
    documented safe no-op and the shared category Budget remains the only gate.
    """
    from .watchdog import deadline_guard

    try:
        limit = float(seconds)
    except (TypeError, ValueError):
        limit = 0.05
    if not math.isfinite(limit):
        # A non-finite per-source budget must never disable the bound; clamp to
        # the tightest safe value instead.
        limit = 0.05
    limit = max(0.05, limit)
    with deadline_guard(limit, lambda: SourceTimeout("source call exceeded wall-clock bound")):
        yield


def safe_source_error(exc: Any, *, kind: str = "source") -> str:
    """Map any failure to a fixed, non-sensitive label.

    Raw mail text, stderr, URLs and credentials must never reach diagnostics or
    persisted coverage, so callers use this instead of ``str(exc)``.
    """
    if isinstance(exc, SourceTimeout):
        return f"{kind} budget-deferred"
    if isinstance(exc, PortalLibraryUnavailable):
        return str(exc)
    text = exc if isinstance(exc, str) else str(exc)
    lowered = text.lower()
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)) or "timeout" in lowered or "timed out" in lowered:
        return f"{kind} timeout"
    if isinstance(exc, FileNotFoundError) or "not found" in lowered:
        return f"{kind} unavailable"
    if any(marker in lowered for marker in ("401", "invalid_grant", "token revoked", "authorization revoked",
                                           "authorisation revoked", "missing configured grant")):
        return "configured authorization requires renewal"
    if "429" in lowered or "rate limit" in lowered:
        from .source_health import retry_after_seconds
        seconds = retry_after_seconds(text)
        return "rate limited 429" + (f"; Retry-After: {seconds}" if seconds else "")
    if any(code in lowered for code in ("503", "502", "504")):
        return f"{kind} provider overload 503"
    if "auth" in lowered or "permission" in lowered or "credential" in lowered or "401" in lowered or "403" in lowered:
        return "authentication unavailable"
    if "parse" in lowered or "json" in lowered or "decode" in lowered:
        return f"{kind} parse error"
    return f"{kind} unavailable"


class SubprocessRunner:
    """Fixed-argv runner. Never uses a shell. Bounded by timeout + output cap."""

    def run(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        proc = subprocess.run(
            list(argv),
            shell=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "")[:MAX_STDOUT], (proc.stderr or "")[:4096]


def _checked_at(clock: Any) -> str:
    return clock.now().isoformat()


class Budget:
    """Wall-clock collector budget. Per-subprocess timeouts are clamped to the
    remaining budget so no source can start a multi-timeout loop and overrun.

    A budget may carry a ``parent`` overall wall-clock. ``remaining`` is then the
    minimum of this category's own reserved allocation and the overall remaining,
    so one category (e.g. core mail) can never consume another category's
    reserved time (e.g. obligations), while the global cap is still enforced.
    """

    def __init__(self, seconds: float, *, parent: "Budget | None" = None, label: str = ""):
        import time

        self.seconds = max(0.0, float(seconds))
        self.parent = parent
        self.label = label
        self._start = time.monotonic()

    def elapsed(self) -> float:
        import time

        return time.monotonic() - self._start

    def own_remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed())

    def remaining(self) -> float:
        own = self.own_remaining()
        if self.parent is not None:
            return min(own, self.parent.remaining())
        return own

    def expired(self) -> bool:
        return self.remaining() <= 0.5

    def restart(self) -> None:
        """Start this category's clock now.

        Used to keep a bounded HTTP category from being silently consumed by a
        preceding non-HTTP phase (e.g. a zero-tool planner), so planning time can
        never make the first public fetch see an already-exhausted budget.
        """
        import time

        self._start = time.monotonic()

    def exclude(self, seconds: float) -> None:
        """Advance this clock to remove a non-HTTP phase from its accounting.

        Unlike :meth:`restart` this PRESERVES any time already charged to the
        category, so a SHARED http budget that ordinary feeds have already spent
        cannot be silently refilled by a later adaptive phase. It is used to keep
        the pre-fetch planner phase from consuming the public HTTP allocation
        while still enforcing the one shared cap.
        """
        import time

        shift = max(0.0, float(seconds))
        if shift <= 0:
            return
        # Never move the start past the present moment: the category's elapsed
        # time is only reduced, never made negative.
        self._start = min(self._start + shift, time.monotonic())

    def clamp(self, cap: float, *, floor: float = 1.0) -> int:
        """Integer per-call timeout clamped to the remaining budget."""
        return max(int(floor), min(int(cap), int(self.remaining())))


def _clamp_timeout(budget: "Budget | None", cap: int) -> int:
    return int(cap) if budget is None else budget.clamp(cap)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    # Himalaya/IMAP envelope dates are commonly RFC2822 (e.g.
    # "Fri, 03 Oct 2026 09:00:00 +0800"). Fall back to the email date parser so
    # a non-ISO mail date is understood instead of silently treated as unknown.
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_sensitive(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(term in lowered for term in _SENSITIVE_TERMS)


def _redact(text: str) -> str:
    # Whole URLs (which may be signed payment/action links) are removed first.
    text = _URL_RE.sub("[redacted-link]", text)
    text = _OTP_RE.sub("[redacted-code]", text)
    return _TOKEN_RE.sub("[redacted-token]", text)


def _bounded_preview(text: str, max_chars: int) -> str:
    collapsed = " ".join(str(text or "").split())
    if _is_sensitive(collapsed):
        return "[redacted: sensitive message]"
    return _redact(collapsed)[: max(1, int(max_chars))]


# -- Himalaya mail (fixed argv, bounded pagination, read-only previews) -------


def _mail_item(account: str, env: dict[str, Any]) -> dict[str, Any]:
    frm = env.get("from") or [{}]
    sender = frm[0] if isinstance(frm, list) and frm else {}
    sender_text = str(sender.get("email") or sender.get("name") or "")
    subject = str(env.get("subject") or "")
    sensitive = _is_sensitive(f"{sender_text} {subject}")
    return {
        "ref": f"mail:{account}:{env.get('id','')}",
        "id": str(env.get("id") or ""),
        "sender": sender_text,
        "subject": "[redacted: sensitive message]" if sensitive else subject,
        "date": str(env.get("date") or ""),
        "has_attachment": bool(env.get("has-attachment")),
        "sensitive": sensitive,
        "preview": "",
        "source": account,
    }


def _attach_previews(
    account: str,
    items: list[dict[str, Any]],
    runner: SubprocessRunner,
    timeout: int,
    preview_limit: int,
    preview_chars: int,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    skip_ids: set[str] | None = None,
    previewed_out: list[str] | None = None,
) -> None:
    attached = 0
    for item in items:
        if predicate is not None and not predicate(item):
            continue  # metadata-first: non-actionable items consume no budget
        mid = str(item.get("id") or "")
        if skip_ids and mid and mid in skip_ids:
            continue  # already previewed in a prior run: never re-read a stable id
        if item.get("sensitive"):
            # Sensitive messages are redacted and never fetched; they must not
            # consume the bounded preview budget.
            item["preview"] = "[redacted: sensitive message]"
            if previewed_out is not None and mid:
                previewed_out.append(mid)
            continue
        if attached >= max(0, int(preview_limit)):
            return
        attached += 1
        # Fixed argv; NO ``--seen`` => the server-side seen flag is untouched.
        argv = ["himalaya", "-a", account, "message", "read", str(item["id"])]
        try:
            code, out, _err = runner.run(argv, timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if code != 0:
            continue
        item["preview"] = _bounded_preview(out, preview_chars)
        if previewed_out is not None and mid:
            previewed_out.append(mid)


def _himalaya_stdout_error(out: str) -> str:
    """Return the safe ``error`` string from a JSON stdout error payload, or ''.

    The Gmail backend reports an unsupported capability as exit 1 with a JSON
    error on **stdout** and an EMPTY stderr (verified live: ``envelope search``
    -> ``{"error":"Gmail does not support the shared envelope search",...}``).
    Callers must recognise that as an unsupported-capability signal (degrade),
    never as an authentication/credential failure.
    """
    text = str(out or "").strip()
    if not text.startswith("{"):
        return ""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return ""
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload.get("error"))
    return ""


def _search_unsupported(error_text: str) -> bool:
    """True when a backend explicitly says the shared envelope search is unsupported.

    This is a capability mismatch (use the plain route), NOT credentials.
    """
    return "does not support the shared envelope search" in str(error_text or "").lower()


def _gmail_unsupported_error(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return "gmail does not support" in lowered


def _native_search_unsupported(error_text: str) -> bool:
    """A NATIVE Gmail capability rejection (metadata format/headers/query)."""
    lowered = str(error_text or "").lower()
    return "not supported" in lowered or "unsupported" in lowered or "invalid query" in lowered


# -- Supported NATIVE Gmail route (users.messages.list/get) -------------------
#
# The shared ``himalaya envelope search`` command is advertised by the installed
# CLI but the Gmail backend REJECTS it at runtime (exit 1, JSON error on stdout,
# empty stderr). The supported, scalable route for these already-authorized
# Gmail accounts is the protocol-native one:
#   list: himalaya -a <acct> gmail messages list --json --query <q> --max-results N
#         -> {"ids":[{"id":<str>,"threadId":<str>}], "next_page":<opaque|null>}
#   get:  himalaya -a <acct> gmail messages get --json --format metadata \
#           --header From --header Subject --header Date <id>
#         -> top-level {id, thread-id, label-ids, snippet, size-estimate,
#                       internal-date, headers:[{name,value}]}  (NO payload wrapper)
# Only ids + a few headers are ever read; the body is never fetched and the
# unused ``snippet`` is discarded by the minimal-field projection below.

_GMAIL_METADATA_FORMAT = "metadata"


def _native_query(cutoff: date) -> str:
    """Fixed Gmail search query built ONLY from a UTC clock date.

    No private data (sender/subject/body) is ever placed in a public/provider
    query; only the bounded UTC lower date bound and the inbox scope.
    """
    return f"in:inbox after:{cutoff.strftime('%Y/%m/%d')}"


def _native_list_argv(account: str, page_size: int, token: str, query: str) -> list[str]:
    argv = [
        "himalaya", "-a", account, "gmail", "messages", "list", "--json",
        "--query", query, "--max-results", str(max(1, min(int(page_size), 100))),
    ]
    if token:
        argv += ["--page-token", str(token)]
    return argv


def _native_get_argv(account: str, message_id: str) -> list[str]:
    # Metadata-only, explicit headers, fixed argv. NO body fetch; read-only.
    return [
        "himalaya", "-a", account, "gmail", "messages", "get", "--json",
        "--format", _GMAIL_METADATA_FORMAT,
        "--header", "From", "--header", "Subject", "--header", "Date",
        str(message_id),
    ]


def _native_ids(payload: Any) -> tuple[list[str], str]:
    """Parse the native list payload: ids are DICTS, ``next_page`` is opaque.

    Returns ``(message_ids, next_token)``. A malformed shape raises ValueError so
    the caller fails closed to a parse error rather than silently claiming an
    empty (complete) window.
    """
    if isinstance(payload, list):
        entries, next_token = payload, None
    elif isinstance(payload, dict):
        entries, next_token = payload.get("ids"), payload.get("next_page")
    else:
        raise ValueError("native payload not an object or list")
    if not isinstance(entries, list):
        raise ValueError("native ids not a list")
    ids: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            ids.append(entry)  # tolerate a plain-string id defensively
        elif isinstance(entry, dict):
            mid = entry.get("id")
            if mid is not None and str(mid):
                ids.append(str(mid))
    token = "" if next_token in (None, "") else str(next_token)
    return ids, token


def _native_header_map(headers: Any) -> dict[str, str]:
    """Map the native top-level ``headers`` list to lower-cased name -> value."""
    out: dict[str, str] = {}
    if isinstance(headers, list):
        for header in headers:
            if isinstance(header, dict):
                name = str(header.get("name") or "").strip().lower()
                if name:
                    out[name] = str(header.get("value") or "")
    return out


def _native_mail_item(account: str, message_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Project a native metadata payload into the minimum retained mail fields.

    Only id, From/Subject/Date are kept; ``snippet`` (which may contain body
    text) and every other unused key are DISCARDED, never persisted.
    """
    headers = _native_header_map(payload.get("headers"))
    sender_text = headers.get("from", "")
    subject = headers.get("subject", "")
    date_text = headers.get("date", "") or str(payload.get("internal-date") or "")
    sensitive = _is_sensitive(f"{sender_text} {subject}")
    return {
        "ref": f"mail:{account}:{message_id}",
        "id": str(message_id),
        "sender": sender_text,
        "subject": "[redacted: sensitive message]" if sensitive else subject,
        "date": date_text,
        "has_attachment": False,  # metadata format exposes no attachment flag
        "sensitive": sensitive,
        "preview": "",
        "source": account,
    }


def collect_himalaya_native(
    account: str,
    limit: int,
    *,
    clock: Any,
    window_hours: int = 2160,
    cutoff_date: str = "",
    page_size: int = 10,
    runner: SubprocessRunner | None = None,
    timeout: int = 20,
    preview_limit: int = 0,
    preview_chars: int = 200,
    preview_selector: Callable[[dict[str, Any]], bool] | None = None,
    preview_timeout: int | None = None,
    max_metadata: int = 3,
    start_token: str = "",
    start_offset: int = 0,
    pending_ids: Sequence[str] | None = None,
    preview_skip_ids: set[str] | None = None,
    query: str = "",
    allowed_accounts: Sequence[str] | None = None,
) -> SourceResult:
    """Supported native Gmail metadata route (bounded, resumable, read-only).

    ``messages.list`` yields a page of ids plus an opaque ``next_page`` token;
    at most ``max_metadata`` metadata ``messages.get`` calls run per account per
    run. The bounded UTC window is either supplied as ``cutoff_date`` (persisted
    stable during bootstrap) or derived from ``clock``/``window_hours``. Only
    minimum extracted fields are returned; raw headers/bodies/snippets never are.

    Stable page-identity queue: ``pending_ids`` is the (narrow) id queue of the
    current provider page. While unvisited ids remain in it the function reuses
    the saved queue and issues NO ``messages list`` call, so provider ordering
    changes and new arrivals at the head cannot shift offsets and skip unvisited
    historical ids. A fresh list only happens once the saved queue is drained,
    and it then uses ``start_token`` (the provider token that follows the drained
    page). Page 2 is therefore only fetched AFTER page 1's queue is fully visited.

    ``scanned`` is the number of ids actually ASSESSED (successful metadata gets
    that produced a projectable item), never the number of ids merely listed
    (``ids_listed``). A failed, unhydrated or id-mismatched get is never counted
    and never consumed.

    Fail-closed: a native capability rejection degrades to the plain envelope
    route; an auth/permission error is NEVER retried with a second command. A
    transient get failure, malformed payload, missing/ill-shaped headers, or an
    exact-id mismatch STOPS the run at the current unvisited id (bounded,
    retryable) and never declares the window complete.
    """
    runner = runner or SubprocessRunner()
    name = f"mail:{account}"
    checked = _checked_at(clock)
    allowed = tuple(allowed_accounts) if allowed_accounts is not None else ALLOWED_MAIL_ACCOUNTS
    if account not in allowed:
        return SourceResult(name, "error", error="account not authorized", checked_at=checked)

    if not cutoff_date:
        cutoff = clock.now().date() - timedelta(hours=max(1, int(window_hours)))
        cutoff_date = cutoff.isoformat()
    try:
        cutoff_dt = datetime.fromisoformat(cutoff_date).replace(tzinfo=timezone.utc)
    except ValueError:
        cutoff_dt = datetime.combine(clock.now().date(), datetime.min.time(), tzinfo=timezone.utc)
        cutoff_date = cutoff_dt.date().isoformat()
    query = query or _native_query(cutoff_dt.date())
    page_size = max(1, min(int(page_size), 100))
    max_metadata = max(0, int(max_metadata))
    start_offset = max(0, int(start_offset))
    # The saved queue is narrow ids only; it is bounded by the requested page
    # size (provider ``--max-results`` <= 100), so it can never grow unbounded.
    queue_in: list[str] = [str(x) for x in pending_ids] if pending_ids is not None else []

    def _degrade(reason: str) -> SourceResult:
        return collect_himalaya(
            account, limit, clock=clock, window_hours=window_hours, page_size=page_size,
            runner=runner, timeout=timeout, preview_limit=preview_limit,
            preview_chars=preview_chars, preview_selector=preview_selector,
            preview_timeout=preview_timeout, preview_skip_ids=preview_skip_ids,
            server_window=False, allowed_accounts=allowed_accounts,
        )

    # Reuse the persisted page queue while it still has unvisited ids. Only when
    # it is drained (or absent) do we issue a bounded list for the next page.
    reuse = bool(queue_in) and start_offset < len(queue_in)
    if reuse:
        ids = list(queue_in)
        listed = 0
        carry_token = str(start_token or "")
    else:
        try:
            code, out, err = runner.run(
                _native_list_argv(account, page_size, start_token, query), timeout)
        except subprocess.TimeoutExpired:
            return SourceResult(name, "error", error="timeout", checked_at=checked,
                                next_token=start_token, offset=start_offset,
                                pending_ids=list(queue_in))
        except FileNotFoundError:
            return SourceResult(name, "error", error="himalaya unavailable", checked_at=checked)

        if code != 0:
            stdout_error = _himalaya_stdout_error(out)
            combined = f"{err} {stdout_error}".strip()
            lowered = combined.lower()
            if any(tok in lowered for tok in ("auth", "credential", "permission", "401", "403")):
                return SourceResult(name, "error", error=safe_source_error(combined, kind="mail"),
                                    checked_at=checked, pending_ids=list(queue_in))
            if _gmail_unsupported_error(stdout_error) or _native_search_unsupported(stdout_error) \
                    or any(tok in lowered for tok in
                           ("subcommand", "unrecognized", "unexpected argument", "not supported",
                            "unsupported")):
                return _degrade(stdout_error or err)
            return SourceResult(name, "error", error=safe_source_error(combined, kind="mail"),
                                checked_at=checked, pending_ids=list(queue_in))

        try:
            payload = json.loads(out)
            ids, carry_token = _native_ids(payload)
        except (ValueError, TypeError):
            # A malformed native shape must NOT masquerade as an empty (complete)
            # window; fail closed as a parse error with no cursor advance.
            return SourceResult(name, "error", error="mail parse error", checked_at=checked,
                                next_token=start_token, offset=start_offset,
                                pending_ids=list(queue_in))
        # A fresh page starts at its own offset zero; a caller-supplied offset
        # (direct resumed call without a persisted queue) is clamped into range.
        start_offset = max(0, min(start_offset, len(ids)))
        listed = len(ids)

    items: list[dict[str, Any]] = []
    attempts = 0          # metadata get attempts (budget)
    metadata_scanned = 0  # successful, projectable gets (assessed)
    stop = False
    fail_reason = ""
    i = start_offset
    while i < len(ids):
        if attempts >= max_metadata:
            break  # metadata budget: i.. are unvisited
        mid = ids[i]
        attempts += 1
        try:
            gcode, gout, gerr = runner.run(_native_get_argv(account, mid), timeout)
        except subprocess.TimeoutExpired:
            stop, fail_reason = True, "mail timeout"
            break
        except FileNotFoundError:
            stop, fail_reason = True, "himalaya unavailable"
            break
        if gcode != 0:
            gout_error = _himalaya_stdout_error(gout)
            combined = f"{gerr} {gout_error}".strip()
            if any(tok in combined.lower()
                   for tok in ("auth", "credential", "permission", "401", "403")):
                # Auth failure on a metadata get is a real error with
                # uncertainty: fail closed, preserve the unvisited id, and never
                # present a fresh/healthy partial or a stale actionable alert.
                return SourceResult(name, "error", error=safe_source_error(combined, kind="mail"),
                                    checked_at=checked, items=items,
                                    truncated=True, next_token=carry_token, offset=i,
                                    pending_ids=list(ids), scanned=metadata_scanned,
                                    ids_listed=listed, failure="authentication unavailable")
            # Transient/unhydrated get failure: keep this id UNVISITED (retry
            # next run), never consume it, never declare the window complete.
            stop, fail_reason = True, safe_source_error(combined, kind="mail")
            break
        try:
            wentry = json.loads(gout)
        except (ValueError, TypeError):
            stop, fail_reason = True, "mail parse error"
            break
        if not isinstance(wentry, dict):
            stop, fail_reason = True, "mail parse error"
            break
        # The exact response id must match the requested id, and the metadata
        # must expose the top-level ``headers`` list; otherwise fail closed.
        resp_id = str(wentry.get("id") or "")
        if resp_id and resp_id != mid:
            stop, fail_reason = True, "mail id mismatch"
            break
        if not isinstance(wentry.get("headers"), list):
            stop, fail_reason = True, "mail parse error"
            break
        items.append(_native_mail_item(account, mid, wentry))
        metadata_scanned += 1
        i += 1
        if len(items) >= max(1, int(limit)):
            break  # item cap reached: i.. may remain

    window_complete = (not stop) and i >= len(ids) and not carry_token
    if window_complete:
        next_token_out, offset_out, pending_out = "", 0, []
    elif i < len(ids):
        # Unvisited ids remain in the saved queue: keep them for the next run.
        next_token_out, offset_out, pending_out = carry_token, i, list(ids)
    else:
        # Queue drained but the provider has a next page token: continue there.
        next_token_out, offset_out, pending_out = carry_token, 0, []

    previewed_out: list[str] = []
    if preview_limit and items:
        _attach_previews(account, items, runner,
                         int(preview_timeout) if preview_timeout else min(int(timeout), 10),
                         preview_limit, preview_chars, predicate=preview_selector,
                         skip_ids=preview_skip_ids, previewed_out=previewed_out)

    return SourceResult(
        name, "ok", count=len(items), items=items, checked_at=checked,
        truncated=not window_complete, next_token=next_token_out, offset=offset_out,
        pending_ids=pending_out,
        window_complete=window_complete, pages_read=1, previewed_ids=previewed_out,
        scanned=metadata_scanned, ids_listed=listed, failure=fail_reason,
    )


def collect_himalaya(
    account: str,
    limit: int,
    *,
    clock: Any,
    window_hours: int = 48,
    page_size: int = DEFAULT_PAGE_SIZE,
    runner: SubprocessRunner | None = None,
    timeout: int = 30,
    preview_limit: int = 0,
    preview_chars: int = 240,
    preview_selector: Callable[[dict[str, Any]], bool] | None = None,
    preview_timeout: int | None = None,
    start_page: int = 1,
    max_pages: int = MAX_PAGES,
    preview_skip_ids: set[str] | None = None,
    server_window: bool = False,
    allowed_accounts: Sequence[str] | None = None,
) -> SourceResult:
    """Bounded Inbox metadata for one authorized Himalaya account (last window).

    The date window is pushed to the backend with a fixed
    ``envelope search ... after <yyyy-mm-dd>`` query. NOTE: the installed CLI
    advertises this command but the Gmail backend REJECTS it at runtime (exit 1,
    JSON error on stdout, empty stderr), so this path effectively always degrades
    to the plain ``envelope list`` argv. The production obligation path uses the
    supported native route (:func:`collect_himalaya_native`); this function's
    ``search`` argv is retained only for the base-mail read and as a bounded
    fallback. When ``server_window`` is False, or when the backend rejects the
    search (including via a stdout JSON error), it degrades to plain
    ``envelope list``.

    Pages newest-first until the mail is older than the window or the item cap is
    hit. Hitting the cap (or exhausting the page budget while mail may remain)
    marks the read ``truncated`` so downstream coverage is partial, not healthy.
    ``start_page``/``max_pages`` allow a resumable historical bootstrap: at most
    ``max_pages`` envelope pages are read per call and ``next_page`` reports the
    cursor for the next run. Only a short redacted preview (never a raw body)
    may be attached, and its timeout is separately capped. ``scanned`` reports
    how many envelope metadata rows were actually examined (safe count).
    """
    runner = runner or SubprocessRunner()
    name = f"mail:{account}"
    checked = _checked_at(clock)
    allowed = tuple(allowed_accounts) if allowed_accounts is not None else ALLOWED_MAIL_ACCOUNTS
    if account not in allowed:
        return SourceResult(name, "error", error="account not authorized", checked_at=checked)

    now = clock.now()
    cutoff = now - timedelta(hours=max(1, int(window_hours)))
    cutoff_date = cutoff.date().isoformat()
    page_size = max(1, min(int(page_size), 100))
    start_page = max(1, int(start_page))
    max_pages = max(1, int(max_pages))
    pages_read = 0
    scanned = 0
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    truncated = False
    complete = False
    unparseable_dates = 0

    def _envelope_argv(page: int, *, use_search: bool) -> list[str]:
        # Fixed argv only. ``search`` pushes the date window to the backend; the
        # trailing ``after <yyyy-mm-dd>`` is the verified shared query DSL.
        if use_search:
            return [
                "himalaya", "-a", account, "envelope", "search", "--json",
                "--page", str(page), "--page-size", str(page_size),
                "after", cutoff_date,
            ]
        return [
            "himalaya", "-a", account, "envelope", "list", "--json",
            "--page", str(page), "--page-size", str(page_size),
        ]
    # A short (non-empty) page is ambiguous under server-side window filtering,
    # so one bounded confirmation read beyond the page budget is allowed. The
    # window is only declared exhausted when that read is empty or yields no new
    # distinct mail; otherwise the confirmation page is the persisted cursor.
    confirm_next = False
    resume_page = 0
    page = start_page

    while True:
        if pages_read >= max_pages and not confirm_next:
            truncated = True  # page budget exhausted while mail may still remain
            break
        pages_read += 1
        searched = bool(server_window)
        argv = _envelope_argv(page, use_search=searched)
        try:
            code, out, err = runner.run(argv, timeout)
            if code != 0 and searched:
                # A backend that does not accept the shared ``search``
                # subcommand/query degrades to the plain list argv for this page,
                # but a genuine auth/permission failure is NEVER retried with a
                # second (potentially expensive) command.
                #
                # The Gmail backend reports this on **stdout** as a JSON error
                # with EMPTY stderr (verified live), so both surfaces must be
                # inspected. This is a capability mismatch, NEVER credentials.
                stdout_error = _himalaya_stdout_error(out)
                if stdout_error and _search_unsupported(stdout_error):
                    code, out, err = runner.run(_envelope_argv(page, use_search=False), timeout)
                else:
                    lowered = f"{err} {stdout_error}".lower()
                    auth_like = any(tok in lowered for tok in
                                    ("auth", "credential", "permission", "401", "403"))
                    unsupported_like = any(tok in lowered for tok in
                                           ("subcommand", "unrecognized", "unexpected argument",
                                            "query", "expected", "invalid"))
                    if unsupported_like and not auth_like:
                        code, out, err = runner.run(_envelope_argv(page, use_search=False), timeout)
        except subprocess.TimeoutExpired:
            return SourceResult(name, "error", error="timeout", checked_at=checked,
                                next_page=page, pages_read=pages_read - 1, scanned=scanned)
        except FileNotFoundError:
            return SourceResult(name, "error", error="himalaya unavailable", checked_at=checked)
        if code != 0:
            return SourceResult(name, "error", error=safe_source_error(err, kind="mail"),
                                checked_at=checked, next_page=page, pages_read=pages_read - 1,
                                scanned=scanned)
        try:
            payload = json.loads(out)
            if isinstance(payload, dict):
                envelopes = payload.get("envelopes")
            elif isinstance(payload, list):
                envelopes = payload
            else:
                envelopes = None
            if not isinstance(envelopes, list):
                raise ValueError("envelopes not a list")
        except (ValueError, KeyError, TypeError):
            return SourceResult(name, "error", error="mail parse error", checked_at=checked,
                                scanned=scanned)
        scanned += len(envelopes)
        if not envelopes:
            complete = True
            break

        had_new = False
        for env in envelopes:
            if not isinstance(env, dict):
                continue
            mid = str(env.get("id") or "")
            if not mid or mid in seen_ids:
                continue
            had_new = True
            when = _parse_time(env.get("date"))
            if when is None and str(env.get("date") or "").strip():
                # A non-empty but unparseable date means the window filter
                # cannot be trusted for this message: keep it (never silently
                # drop evidence) but mark the read partial.
                unparseable_dates += 1
            if when is not None and when < cutoff:
                complete = True  # newest-first: everything after is older too
                break
            seen_ids.add(mid)
            items.append(_mail_item(account, env))
            if len(items) >= max(1, int(limit)):
                break

        if complete:
            break
        if len(items) >= max(1, int(limit)):
            truncated = True  # cap hit; older/newer in-window mail may remain
            break
        if confirm_next:
            # This was the single confirmation read after a short page.
            if not had_new:
                complete = True  # no further distinct mail: window exhausted
            else:
                truncated = True  # genuine more mail: resume from this page
                resume_page = page
            break
        if len(envelopes) < page_size:
            confirm_next = True  # read one page past the budget to confirm the end
        page += 1

    if unparseable_dates:
        truncated = True  # window coverage cannot be declared complete

    previewed_out: list[str] = []
    if preview_limit and items:
        _attach_previews(account, items, runner,
                         int(preview_timeout) if preview_timeout else min(int(timeout), 10),
                         preview_limit, preview_chars, predicate=preview_selector,
                         skip_ids=preview_skip_ids, previewed_out=previewed_out)

    window_complete = complete and not truncated and not unparseable_dates
    next_page = 0 if complete else (resume_page or start_page + pages_read)
    return SourceResult(name, "ok", count=len(items), items=items, checked_at=checked,
                        truncated=not window_complete, next_page=next_page,
                        window_complete=window_complete, pages_read=pages_read,
                        previewed_ids=previewed_out, scanned=scanned)


# -- Portal-backed Google sources (lazy import; shipped library untouched) --

_GOOGLE_CACHE: dict[str, Any] = {}


def _google_access_token(alias: str) -> str:
    cached = _GOOGLE_CACHE.get(alias)
    if cached:
        return cached
    _insert_secretary_src()
    from secretary.cli import _google, _load_config  # type: ignore
    from secretary.oauth import TokenManager  # type: ignore

    token = _google(_load_config(None), TokenManager()).access_token(alias)
    _GOOGLE_CACHE[alias] = token
    return token


def _google_aliases(policy: dict[str, Any] | None = None) -> list[str]:
    """Authorized Google account aliases (primary + configured aliases).

    Availability discovery is separate from explicit configured access: when a
    validated user configuration names calendar accounts, only those are read
    (an empty configured list means none). Without that key the legacy
    library-discovered aliases are preserved.
    """
    configured = configured_calendar_accounts(policy)
    if configured is not None:
        return list(configured)
    try:
        _insert_secretary_src()
        from secretary.oauth import GOOGLE_ALIASES  # type: ignore

        aliases = sorted(str(alias) for alias in GOOGLE_ALIASES)
        return aliases or ["primary"]
    except Exception:  # noqa: BLE001 - never let alias discovery break collection
        return ["primary"]


def _calendar_client(alias: str) -> Any:
    _insert_secretary_src()
    from secretary.google import CalendarClient  # type: ignore

    return CalendarClient(_google_access_token(alias))


def _tasks_client(alias: str) -> Any:
    _insert_secretary_src()
    from secretary.google import TasksClient  # type: ignore

    return TasksClient(_google_access_token(alias))


def _selected_calendars(client: Any) -> list[dict[str, Any]]:
    calendars = client.calendars().get("items", [])
    return [
        cal for cal in calendars
        if isinstance(cal, dict) and (
            cal.get("primary") or cal.get("selected") or client._is_holiday_calendar(cal)
        )
    ]


def _calendar_batch(
    client: Any,
    calendar_id: str,
    time_min: str,
    time_max: str,
    *,
    remaining: int,
    page_size: int,
    event_types: tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Page one calendar with ``nextPageToken``; stop at ``remaining`` items."""
    out: list[dict[str, Any]] = []
    token: str | None = None
    per_page = max(1, min(int(page_size), 250))
    while True:
        params: list[tuple[str, str]] = [
            ("timeMin", time_min),
            ("timeMax", time_max),
            ("singleEvents", "true"),
            ("orderBy", "startTime"),
            ("showDeleted", "true"),  # keep cancelled events as status evidence
            ("maxResults", str(per_page)),
        ]
        params.extend(("eventTypes", et) for et in (event_types or ()))
        if token:
            params.append(("pageToken", token))
        response = client._request("GET", f"{client._events(calendar_id)}?{urlencode(params)}")
        for event in response.get("items") or []:
            if isinstance(event, dict):
                out.append(event)
        token = response.get("nextPageToken")
        if not token:
            return out, False
        if len(out) >= remaining:
            return out, True


def _event_start_key(event: dict[str, Any]) -> tuple[int, str]:
    # Absorbed items carry a normalized ISO string, while raw Google events
    # carry a start object. Support both without inventing a time.
    start = event.get("start")
    raw = (start.get("dateTime") or start.get("date")) if isinstance(start, dict) else start
    parsed = _parse_time(raw)
    if parsed is None:
        return (1, "")
    return (0, parsed.astimezone(timezone.utc).isoformat())


def _calendar_item(alias: str, calendar_id: str, calendar: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start") or {}
    end = event.get("end") or {}
    return {
        "ref": f"cal:{alias}:{calendar_id}:{event.get('id','')}",
        "id": str(event.get("id") or ""),
        "title": str(event.get("summary") or "(untitled event)"),
        "start": str(start.get("dateTime") or start.get("date") or ""),
        "end": str(end.get("dateTime") or end.get("date") or ""),
        "all_day": bool(start.get("date") and not start.get("dateTime")),
        "status": str(event.get("status") or "confirmed"),
        "calendar": calendar_id,
        "calendar_summary": str(calendar.get("summary") or calendar_id),
        "updated": str(event.get("updated") or ""),
        "source": alias,
        # Ephemeral venue/GEO used only by the opt-in local phone-context module
        # (eligibility + local destination resolution). Never serialized into the
        # evidence packet or state (see candidates.build_packet and location.py).
        "location": str(event.get("location") or ""),
    }


def collect_calendar(
    alias: str,
    *,
    clock: Any,
    horizon_hours: int = 48,
    max_items: int = 20,
    timeout: int = 30,
    page_size: int = DEFAULT_PAGE_SIZE,
    calendar_ids: list[str] | None = None,
) -> SourceResult:
    name = f"calendar:{alias}"
    checked = _checked_at(clock)
    missing = secretary_library_error()
    if missing:
        return SourceResult(name, "error", error=missing, checked_at=checked)
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    truncated = False
    try:
        with wallclock_alarm(timeout):
            client = _calendar_client(alias)
            now = clock.now()
            time_min = now.isoformat()
            time_max = (now + timedelta(hours=max(1, int(horizon_hours)))).isoformat()
            selected = _selected_calendars(client)
            if calendar_ids:
                selected = [cal for cal in selected if cal.get("id") in calendar_ids]

            def absorb(calendar_id: str, calendar: dict[str, Any], batch: list[dict[str, Any]]) -> None:
                for event in batch:
                    key = (calendar_id, str(event.get("id")))
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append(_calendar_item(alias, calendar_id, calendar, event))

            for cal in selected:
                cid = cal.get("id")
                if not isinstance(cid, str) or not cid:
                    continue
                remaining = max_items - len(events)
                if remaining <= 0:
                    truncated = True
                    break
                batch, more = _calendar_batch(client, cid, time_min, time_max,
                                              remaining=remaining, page_size=page_size)
                absorb(cid, cal, batch)
                if more:
                    truncated = True
                    if len(events) >= max_items:
                        break

            # Birthdays are surfaced from the primary calendar as their own pass.
            for cal in selected:
                if not cal.get("primary"):
                    continue
                remaining = max_items - len(events)
                if remaining <= 0:
                    truncated = True
                    break
                batch, more = _calendar_batch(client, cal["id"], time_min, time_max,
                                              remaining=remaining, page_size=page_size,
                                              event_types=("birthday",))
                for event in batch:
                    key = (str(cal["id"]), str(event.get("id")))
                    if key in seen:
                        continue
                    seen.add(key)
                    item = _calendar_item(alias, str(cal["id"]), cal, event)
                    item["birthday"] = True
                    events.append(item)
                if more:
                    truncated = True
    except SourceTimeout:
        # Budget expiry, not an auth failure: preserve whatever was collected
        # and mark the source partial/budget-deferred.
        events.sort(key=_event_start_key)
        return SourceResult(name, "ok", count=len(events), items=events[:max_items],
                            checked_at=checked, truncated=True,
                            failure="calendar budget-deferred")
    except Exception as exc:  # noqa: BLE001 - surface as coverage failure
        return SourceResult(name, "error", error=safe_source_error(exc, kind="calendar"),
                            checked_at=checked)
    events.sort(key=_event_start_key)
    events = events[:max_items]
    return SourceResult(name, "ok", count=len(events), items=events, checked_at=checked,
                        truncated=truncated)


def _tasklists_paged(client: Any, *, max_lists: int = 50, page_size: int = DEFAULT_PAGE_SIZE) -> tuple[list[dict[str, Any]], bool]:
    lists: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params = [("maxResults", str(max(1, min(int(page_size), 100))))]
        if token:
            params.append(("pageToken", token))
        response = client._request("GET", f"{client.base}/users/@me/lists?{urlencode(params)}")
        for item in response.get("items") or []:
            if isinstance(item, dict):
                lists.append(item)
        token = response.get("nextPageToken")
        if not token:
            return lists, False
        if len(lists) >= max_lists:
            return lists, True


def _tasks_paged(
    client: Any,
    tasklist_id: str,
    *,
    due_max: str,
    remaining: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], bool]:
    out: list[dict[str, Any]] = []
    token: str | None = None
    per_page = max(1, min(int(page_size), 100))
    while True:
        params = [("maxResults", str(per_page)), ("showHidden", "true")]
        if due_max:
            params.append(("dueMax", due_max))
        if token:
            params.append(("pageToken", token))
        response = client._request("GET", f"{client._tasks(tasklist_id)}?{urlencode(params)}")
        for task in response.get("items") or []:
            if isinstance(task, dict):
                out.append(task)
        token = response.get("nextPageToken")
        if not token:
            return out, False
        if len(out) >= remaining:
            return out, True


def _task_item(alias: str, task: dict[str, Any], due_date: date | None) -> dict[str, Any]:
    return {
        "ref": f"task:{alias}:{task.get('_list_id','')}:{task.get('id','')}",
        "id": str(task.get("id") or ""),
        "title": str(task.get("title") or "(untitled task)"),
        # date-only semantics: never invent a clock time for a due date
        "due_date": due_date.isoformat() if due_date else "",
        "status": str(task.get("status") or ""),
        "tasklist": str(task.get("_list_id") or ""),
        "updated": str(task.get("updated") or ""),
        "source": alias,
    }


def collect_tasks(
    alias: str,
    *,
    clock: Any,
    horizon_days: int = 2,
    max_items: int = 20,
    timeout: int = 30,
    page_size: int = DEFAULT_PAGE_SIZE,
    task_lists: list[str] | None = None,
    task_adapter: str = "",
) -> SourceResult:
    name = f"tasks:{alias}"
    checked = _checked_at(clock)
    if task_adapter not in {"", "tasks", "productivity_secretary"}:
        return SourceResult(name, "error", error="unsupported tasks adapter", checked_at=checked)
    missing = secretary_library_error()
    if missing:
        return SourceResult(name, "error", error=missing, checked_at=checked)
    raw: list[dict[str, Any]] = []
    items_out: list[dict[str, Any]] = []
    try:
        with wallclock_alarm(timeout):
            client = _tasks_client(alias)
            now = clock.now()
            today = now.date()
            horizon = today + timedelta(days=max(0, int(horizon_days)))
            due_max = (datetime.combine(horizon + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
                       .isoformat().replace("+00:00", "Z"))
            lists, lists_truncated = _tasklists_paged(client, page_size=page_size)
            truncated = lists_truncated
            raw_cap = max(max_items, max_items * 3)
            for tasklist in lists:
                # Bare entries are exact IDs or exact titles (case-sensitive).
                # id:/name: disambiguate when an ID and title collide.
                if task_lists and not any(
                    (entry[3:] == tasklist.get("id") if entry.startswith("id:") else
                     entry[5:] == tasklist.get("title") if entry.startswith("name:") else
                     entry in (tasklist.get("id"), tasklist.get("title")))
                    for entry in task_lists
                ):
                    continue
                if not isinstance(tasklist.get("id"), str) or not tasklist["id"]:
                    continue
                remaining = raw_cap - len(raw)
                if remaining <= 0:
                    truncated = True
                    break
                batch, more = _tasks_paged(client, str(tasklist.get("id")), due_max=due_max,
                                           remaining=remaining, page_size=page_size)
                for task in batch:
                    task = dict(task)
                    task["_list_id"] = str(tasklist.get("id") or "")
                    raw.append(task)
                if more:
                    truncated = True
                    break

            for task in raw:
                if task.get("status") != "needsAction":
                    continue
                due = str(task.get("due") or "")
                due_date: date | None = None
                if len(due) >= 10:
                    try:
                        due_date = datetime.strptime(due[:10], "%Y-%m-%d").date()
                    except ValueError:
                        due_date = None
                if due_date is not None and due_date > horizon:
                    continue
                items_out.append(_task_item(alias, task, due_date))
                if len(items_out) >= max_items:
                    truncated = True
                    break
    except SourceTimeout:
        return SourceResult(name, "ok", count=len(items_out), items=items_out,
                            checked_at=checked, truncated=True,
                            failure="tasks budget-deferred")
    except Exception as exc:  # noqa: BLE001
        return SourceResult(name, "error", error=safe_source_error(exc, kind="tasks"),
                            checked_at=checked)
    return SourceResult(name, "ok", count=len(items_out), items=items_out, checked_at=checked,
                        truncated=truncated)


# -- Optional bounded last-DM context (opt-in; never dumps conversation) -------

_PREF_HINTS = ("stop sending", "don't send", "dont send", "suppress", "remind me",
               "snooze", "career updates", "unless exceptionally", "quiet hours")


def collect_last_dm_preferences(
    *,
    sessions_dir: str | None = None,
    max_lines: int = 5,
    max_chars: int = 200,
) -> list[str]:
    """Bounded, read-only extraction of preference-like statements.

    Disabled unless an explicit ``sessions_dir`` is supplied. Scans only the most
    recent session file, returns at most ``max_lines`` short matched lines, and
    never returns unrelated conversation. Unavailable/unreadable -> ``[]``.
    """
    if not sessions_dir:
        return []
    from pathlib import Path

    root = Path(sessions_dir)
    if not root.is_dir():
        return []
    try:
        candidates = sorted(
            (p for p in root.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:1]
    except OSError:
        return []
    found: list[str] = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for raw in text.splitlines():
            line = " ".join(raw.split())
            lowered = line.lower()
            if not line or len(line) > max_chars:
                continue
            if any(hint in lowered for hint in _PREF_HINTS):
                found.append(_redact(line)[:max_chars])
            if len(found) >= max_lines:
                return found
    return found


def default_sources(policy: dict[str, Any], clock: Any,
                    budget: "Budget | None" = None) -> list[Callable[[], SourceResult]]:
    horizon = int(policy.get("calendar_horizon_hours", 48))
    task_days = int(policy.get("task_horizon_days", 2))
    mail_limit = int(policy.get("max_mail_items", 15))
    mail_window = int(policy.get("mail_window_hours", 48))
    cal_limit = int(policy.get("max_calendar_items", 20))
    task_limit = int(policy.get("max_task_items", 20))
    page_size = int(policy.get("source_page_size", DEFAULT_PAGE_SIZE))
    # Routine base mail is metadata-first: at most one short preview per account
    # (2 s), because content is only needed for the dedicated obligation flow.
    previews = int(policy.get("mail_preview_items", 1))
    preview_timeout = int(policy.get("mail_preview_timeout_seconds", 2))

    factories: list[Callable[[], SourceResult]] = []
    calendar_on = source_enabled(policy, "calendar_enabled")
    tasks_on = source_enabled(policy, "tasks_enabled")
    mail_on = source_enabled(policy, "mail_enabled")
    mail_accounts = configured_mail_accounts(policy)

    # Optional Google Workspace calendar adapter: only when explicitly enabled
    # AND explicit calendars are configured. A source call happens only here,
    # never during registration/doctor probing.
    gws_calendar_ids = list(policy.get("calendar_ids") or [])
    if calendar_on and policy.get("gws_calendar_enabled") and gws_calendar_ids:
        from .gws_calendar import GwsCalendarAdapter

        adapter = GwsCalendarAdapter(policy)
        for calendar_id in gws_calendar_ids:
            factories.append(
                lambda calendar_id=calendar_id: _budgeted(
                    f"calendar:gws:{calendar_id}", budget,
                    lambda: adapter.collect(clock, {**policy, "calendar_ids": [calendar_id]},
                                            budget=budget),
                    dispatch=policy.get("_source_dispatch"),
                )
            )

    compatibility_on = policy.get("productivity_secretary_enabled", True)
    if calendar_on and compatibility_on:
        for alias in _google_aliases(policy):
            factories.append(
                lambda alias=alias: _budgeted(
                    f"calendar:{alias}", budget,
                    lambda: collect_calendar(
                        alias, clock=clock, horizon_hours=horizon, max_items=cal_limit,
                        page_size=page_size, timeout=_clamp_timeout(budget, 25),
                        calendar_ids=policy.get("calendar_ids"),
                    ),
                    dispatch=policy.get("_source_dispatch"),
                )
            )
    if tasks_on and compatibility_on:
        for alias in _task_aliases(policy):
            factories.append(
                lambda alias=alias: _budgeted(
                    f"tasks:{alias}", budget,
                    lambda: collect_tasks(
                        alias, clock=clock, horizon_days=task_days, max_items=task_limit,
                        page_size=page_size, timeout=_clamp_timeout(budget, 25),
                        task_lists=policy.get("task_lists"),
                        task_adapter=policy.get("task_adapter", ""),
                    ),
                    dispatch=policy.get("_source_dispatch"),
                )
            )
    if mail_on:
        for account in mail_accounts:
            factories.append(
                lambda account=account: _budgeted(
                    f"mail:{account}", budget,
                    lambda: collect_himalaya(
                        account, mail_limit, clock=clock, window_hours=mail_window,
                        page_size=page_size, preview_limit=previews,
                        preview_timeout=preview_timeout,
                        timeout=_clamp_timeout(budget, 20), max_pages=1,
                        # The Gmail backend rejects the shared ``envelope search``
                        # command, so production base mail goes straight to the
                        # supported plain envelope list (no wasted failed call).
                        server_window=False,
                        allowed_accounts=mail_accounts,
                    ),
                    dispatch=policy.get("_source_dispatch"),
                )
            )
    return factories


def _budgeted(name: str, budget: "Budget | None", fn: Callable[[], SourceResult],
              *, dispatch=None) -> SourceResult:
    """Run a source only while budget remains; otherwise declare it partial."""
    if budget is not None and budget.expired():
        return SourceResult(name, "ok", count=0, items=[], checked_at="", truncated=True,
                            failure="collector budget exhausted before this source")
    if dispatch is not None and not dispatch(name):
        return SourceResult(name, "deferred", failure="source-health cooldown", attempted=False)
    return fn()


def expanded_sources(
    policy: dict[str, Any],
    clock: Any,
    *,
    store: Any | None = None,
    context: list[str] | None = None,
    budget: "Budget | None" = None,
) -> list[Callable[[], SourceResult]]:
    """Core sources plus the public-feed and obligation pipelines end-to-end.

    Lazy imports keep the core collectors free of a hard dependency on the
    expansion modules.
    """
    from .obligations import collect_obligations
    from .publicfeeds import collect_public_feeds

    factories = list(default_sources(policy, clock, budget=budget))
    if policy.get("feeds_enabled", True):
        factories.append(
            lambda: collect_public_feeds(policy, clock, store=store, context=context, budget=budget)
        )
    if policy.get("obligations_enabled", True):
        # Obligations return one result per account; collapse for the single-
        # result factory contract while preserving kind "obligation".
        factories.append(
            lambda: _as_single(collect_obligations(policy, clock, store=store, budget=budget))
        )
    return factories


def _as_single(results: list[SourceResult]) -> SourceResult:
    """Collapse multi-account results for the single-result factory contract."""
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    truncated = False
    checked = ""
    scanned = 0
    ids_listed = 0
    pages_read = 0
    window_complete = True
    next_token = ""
    offset = 0
    pending_ids: list[str] = []
    for res in results:
        items.extend(res.items)
        truncated = truncated or res.truncated
        checked = checked or res.checked_at
        scanned += int(getattr(res, "scanned", 0) or 0)
        ids_listed += int(getattr(res, "ids_listed", 0) or 0)
        pages_read += int(getattr(res, "pages_read", 0) or 0)
        window_complete = window_complete and bool(getattr(res, "window_complete", False))
        next_token = next_token or str(getattr(res, "next_token", "") or "")
        offset = offset or int(getattr(res, "offset", 0) or 0)
        if not pending_ids:
            pending_ids = list(getattr(res, "pending_ids", []) or [])
        if not res.healthy:
            errors.append(f"{res.name}:{res.error}")
    if errors and not items:
        return SourceResult("obligation", "error", error="; ".join(errors)[:200], checked_at=checked)
    return SourceResult("obligation", "ok" if not errors else "stale", count=len(items),
                        items=items, checked_at=checked, truncated=truncated,
                        failure="; ".join(errors)[:200], scanned=scanned,
                        ids_listed=ids_listed, pages_read=pages_read,
                        window_complete=window_complete, next_token=next_token,
                        offset=offset, pending_ids=pending_ids)
