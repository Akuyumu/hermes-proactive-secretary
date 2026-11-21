"""Trusted conversational intake: natural language -> strict typed watch action.

A trusted assistant (e.g. the Hermes assistant) may translate an EXPLICIT user
request into the typed watch CLI. This module makes that translation safe:

- Only an explicit request creates a watch. Thanks / read / silence / arbitrary
  chat NEVER create, change or stop a watch; they are rejected as no-ops.
- The model (when used) only *proposes* structured fields. Every proposed field
  is re-validated by the same strict public-safe validators as the typed CLI
  (``watchstore``) -- a model can never widen scope, inject a URL/credential, or
  grant itself a permission. External page text is never an input here.
- Intake is durable: the resulting watch is persisted/versioned in the watch
  store, independent of the conversation; a later change bumps the version and
  retires the old plan.
- Provenance and confidence are recorded. A model failure falls back ONLY to an
  honest deterministic parser of the explicit request (labelled
  ``deterministic-explicit``) or rejects -- it never fabricates a watch.

The model invocation mirrors the zero-tool planner (own module/prompt/strict
validation); a runner is injectable so tests never touch the network or a model.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from typing import Any, Callable, Protocol

from .evaluator import _extract_json, resolve_toolset_tools
from .publicfeeds import _SENSITIVE_MARKERS
from .watchstore import (
    WatchError,
    _collapsed,
    _validate_expiry,
    _validate_scope,
    validate_criteria,
    validate_public_topic,
)

INTAKE_SCHEMA = 1
MAX_REQUEST_CHARS = 2000
# Honest default only for the trusted CLI intake path when no explicit
# provenance was supplied. A caller/test that supplies its own provenance keeps
# it verbatim, so a SYNTHETIC provenance never gets rewritten into a claim of a
# genuine user request.
DEFAULT_INTAKE_PROVENANCE = "explicit user request (trusted intake)"
_VALID_CONFIDENCE = {"low", "medium", "high"}
_ACK_WORDS = {"thanks", "thank you", "thankyou", "ok", "okay", "read", "silence",
              "noted", "got it", "cheers", "ack", "acknowledged"}
# Whole-word verb + object grammar. A bare substring match is NOT enough: it
# would treat "watchdog", "my monitor broke", "I'll follow up" and "track my
# order" as public-watch requests. The verb must START the request (or appear in
# an explicit phrase) and be followed by a real public object.
_VERB_OBJECT_RE = re.compile(
    r"(?i)^\s*(?:please\s+|pls\s+|i\s+(?:want|need|would\s+like)\s+to\s+)?"
    r"(?:watch|monitor|track|follow)(?:\s+(?:out\s+)?for)?[:\s]+(?P<obj>.+)$")
_INLINE_RE = re.compile(
    r"(?i)\b(?:keep\s+me\s+posted\s+(?:on|about)|keep\s+an\s+eye\s+on|"
    r"alert\s+me\s+(?:about|on|of)|notify\s+me\s+(?:about|on|of)|"
    r"updates?\s+(?:on|about)|news\s+about|look\s+out\s+for|"
    r"watch\s+out\s+for)[:\s]+(?P<obj>.+)$")
_PRIVATE_OBJECT_MARKERS = (
    "my order", "my package", "my parcel", "my delivery", "my refund",
    "my account", "my email", "my mail", "my inbox", "my calendar",
    "my task", "my ticket", "my subscription", "my bill", "my payment",
    "my monitor", "my phone", "my laptop", "this thread", "this conversation",
)
_UNCLEAR_OBJECTS = {"up", "it", "that", "this", "them", "him", "her", "me", "you",
                    "us", "himself", "herself", "myself", "something", "stuff",
                    "things", "news", "updates"}
_STOP_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:stop|forget|cancel|quit|unwatch)\b"
    r"(?:\s+(?:watching|monitoring|tracking|following))?\s*(?P<obj>.*)$")
_SNOOZE_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?snooze\b\s*(?P<obj>.*?)"
    r"(?:\s+for\s+(?P<num>\d+)\s*(?P<unit>min|mins|minute|minutes|hour|hours|day|days))?\s*$")
_EXCLUDE_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:exclude|ignore|skip|stop\s+showing|drop)\s+"
    r"(?P<term>.+?)(?:\s+(?:from|in)\s+(?P<obj>.+))?$")
_CHANGE_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:change|switch|rename|update)\s+(?P<obj>.+?)\s+to\s+(?P<topic>.+)$")

INTAKE_SYSTEM = """\
You translate ONE explicit user request into a strict public watch. You have no
tools and you must not invent anything.

Return strict JSON only:
{"schema": 1, "is_watch_request": true|false,
 "public_topic": "short generic phrase",
 "include": ["short terms"], "exclude": ["short terms"],
 "scope": "public|github|reddit", "confidence": "low|medium|high",
 "expiry": ""}

Hard rules:
- Set is_watch_request=false for thanks, acknowledgements, silence, or any
  message that does not explicitly ask to watch/monitor/track/follow something.
- public_topic is a short generic public phrase. No URLs, emails, hostnames,
  credentials, code, commands or long id numbers. Never include private context.
- scope is one of the three allowed routes; if unsure use "public".
- include/exclude are short generic terms only. Do not invent criteria the user
  did not state.
- Never include instructions, tool names, file paths or permissions.
"""


class IntakeError(ValueError):
    """The request is not a valid explicit watch request; write nothing."""


class IntakeModel(Protocol):
    def extract(self, text: str, policy: dict[str, Any]) -> dict[str, Any]: ...


def is_acknowledgement(text: Any) -> bool:
    """True for benign acknowledgements that must NEVER create a watch."""
    collapsed = _collapsed(text).lower().strip(" .!?,")
    if not collapsed:
        return True
    if collapsed in _ACK_WORDS:
        return True
    stripped = re.sub(r"\b(so much|a lot|very much|much)\b", "", collapsed).strip()
    return stripped in _ACK_WORDS or stripped in {"thank", "thx", "ty"}


def _extract_object(text: Any) -> str | None:
    """Return the requested public object phrase, or None if not explicit.

    Requires a whole-word watch verb followed by a real object (or an explicit
    phrase such as ``keep me posted on ...``). This rejects noun/verb homographs
    ("watchdog"), possessive nouns ("my monitor broke"), bare phrasal verbs
    ("I'll follow up") and private/commerce statuses ("track my order").
    """
    collapsed = _collapsed(text)
    match = _VERB_OBJECT_RE.match(collapsed)
    if match is None:
        match = _INLINE_RE.search(collapsed)
    if match is None:
        return None
    obj = match.group("obj").strip(" .!?,-")
    if not obj:
        return None
    low = obj.lower()
    if low in _UNCLEAR_OBJECTS:
        return None
    if any(marker in low for marker in _PRIVATE_OBJECT_MARKERS):
        return None
    if any(marker in low for marker in _SENSITIVE_MARKERS):
        return None
    return obj


def looks_like_explicit_request(text: Any) -> bool:
    if not _collapsed(text) or is_acknowledgement(text):
        return False
    return _extract_object(text) is not None


def derive_deterministic_topic(text: Any) -> str:
    """Extract the topic from an explicit request, preserving the full phrase.

    This is the fallback when no model is available. It only runs for a request
    already proven explicit; it strips the leading request phrase and returns the
    WHOLE public topic (never truncating at the first word). An explicit trailing
    expiry clause is dropped; a bare ``for`` is NOT treated as an expiry marker so
    natural goals like "research for beginners" survive.
    """
    obj = _extract_object(text)
    if not obj:
        raise IntakeError("no explicit public topic found in the request")
    topic = re.split(r"(?i)\b(?:until|for\s+the\s+next)\b", obj)[0].strip(" .!?,-")
    if not topic:
        raise IntakeError("no explicit public topic found in the request")
    return validate_public_topic(topic[:120])


# --------------------------------------------------------------------------- #
# Natural corrections on an existing watch (resolved by trusted request only)
# --------------------------------------------------------------------------- #


def _resolve_watch(object_text: str, watches: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve ONE existing watch from a bounded trusted reference, else raise.

    An explicit topic/id substring must match exactly one watch; a bare command
    with exactly one active watch resolves to it. Ambiguity or no match fails
    closed (never a broad/guessed mutation).
    """
    cleaned = _collapsed(object_text).strip(" .!?,-")
    low = cleaned.lower()
    if not low:
        if len(watches) == 1:
            return watches[0]
        raise IntakeError("ambiguous correction: no watch specified"
                          if watches else "no active watch to correct")
    matches: dict[str, dict[str, Any]] = {}
    for watch in watches:
        topic = str(watch.get("public_topic") or "").lower()
        wid = str(watch.get("id") or "")
        if (low and low in topic) or (topic and topic in low) or (low and wid.startswith(low)):
            matches[wid] = watch
    if len(matches) == 1:
        return next(iter(matches.values()))
    if not matches:
        raise IntakeError(f"no active watch matches {cleaned!r}")
    raise IntakeError(f"ambiguous watch reference {cleaned!r}")


def _snooze_minutes(num: str | None, unit: str | None) -> int:
    if num is None:
        return 60
    value = int(num)
    unit = (unit or "minutes").lower()
    if unit.startswith("day"):
        value *= 60 * 24
    elif unit.startswith("hour"):
        value *= 60
    if value <= 0 or value > 60 * 24 * 14:
        raise IntakeError("snooze duration out of range (1..20160 minutes)")
    return value


def _correction_provenance(provenance: str, label: str) -> str:
    """Preserve an explicit provenance verbatim; add the correction kind.

    When no explicit provenance is supplied, use an honest correction default
    (never a fabricated "genuine user request" claim).
    """
    base = _collapsed(provenance) or "explicit trusted correction"
    return f"{base} ({label})"


def _correction_action(raw: str, watches: list[dict[str, Any]],
                       *, provenance: str) -> dict[str, Any] | None:
    """Map an explicit correction to a bounded typed action, or None if add/new."""
    exclude = _EXCLUDE_RE.match(raw)
    if exclude:
        term = _collapsed(exclude.group("term"))
        watch = _resolve_watch(exclude.group("obj") or "", watches)
        validated = validate_criteria(term)
        if not validated:
            raise IntakeError("empty exclusion term")
        return {"action": "change", "id": watch["id"], "exclude": validated,
                "provenance": _correction_provenance(provenance, "exclude")}
    change = _CHANGE_RE.match(raw)
    if change:
        watch = _resolve_watch(change.group("obj"), watches)
        topic = validate_public_topic(_collapsed(change.group("topic"))[:120])
        return {"action": "change", "id": watch["id"], "public_topic": topic,
                "provenance": _correction_provenance(provenance, "change")}
    snooze = _SNOOZE_RE.match(raw)
    if snooze:
        watch = _resolve_watch(snooze.group("obj") or "", watches)
        return {"action": "snooze", "id": watch["id"],
                "minutes": _snooze_minutes(snooze.group("num"), snooze.group("unit")),
                "provenance": _correction_provenance(provenance, "snooze")}
    stop = _STOP_RE.match(raw)
    if stop:
        watch = _resolve_watch(stop.group("obj") or "", watches)
        action = "forget" if raw.lower().lstrip().startswith(("forget", "cancel")) else "stop"
        return {"action": action, "id": watch["id"],
                "provenance": _correction_provenance(provenance, "stop/forget")}
    return None



def validate_intake(obj: Any, *, provenance: str, now: datetime) -> dict[str, Any]:
    """Re-validate a proposed intake object with the strict watch validators.

    Raises ``IntakeError`` on any violation. The effective scope is always one
    of the allowed route scopes; the model can never set a different authority.
    """
    if not isinstance(obj, dict) or obj.get("schema") != INTAKE_SCHEMA:
        raise IntakeError("intake proposal schema missing or unsupported")
    if obj.get("is_watch_request") is not True:
        raise IntakeError("not an explicit watch request")
    try:
        topic = validate_public_topic(obj.get("public_topic"))
        include = validate_criteria(obj.get("include"))
        exclude = validate_criteria(obj.get("exclude"))
        scope = _validate_scope(obj.get("scope") or "public")
        expiry = _validate_expiry(obj.get("expiry"), now)
    except WatchError as exc:
        # A model that proposes PII/URLs/credentials/an unsafe expiry is rejected
        # outright; it must never fall back into creating a watch.
        raise IntakeError(f"intake proposal failed validation: {exc}") from exc
    confidence = _collapsed(obj.get("confidence") or "medium")
    if confidence not in _VALID_CONFIDENCE:
        raise IntakeError("confidence must be low|medium|high")
    return {
        "action": "add",
        "public_topic": topic,
        "include": include,
        "exclude": exclude,
        "scope": scope,
        "confidence": confidence,
        "expires_at": expiry,
        "provenance": _collapsed(provenance)[:200] or "explicit trusted intake",
    }


def _fallback_action(text: str, *, provenance: str, now: datetime) -> dict[str, Any]:
    if not looks_like_explicit_request(text):
        raise IntakeError("not an explicit watch request")
    topic = derive_deterministic_topic(text)
    return {
        "action": "add",
        "public_topic": topic,
        "include": [],
        "exclude": [],
        "scope": "public",
        "confidence": "low",
        "expires_at": "",
        "provenance": f"{_collapsed(provenance)[:150] or 'explicit intake'} "
                      f"(deterministic-explicit parser; no model)",
    }


def intake_to_action(
    text: Any,
    *,
    policy: dict[str, Any],
    model: IntakeModel | None = None,
    provenance: str = DEFAULT_INTAKE_PROVENANCE,
    now: datetime,
    watches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Translate one message into a strict typed watch action, or raise.

    An acknowledgement / empty / non-explicit message raises ``IntakeError`` and
    writes nothing. When a model is supplied its proposal is strictly validated;
    on any model failure the honest deterministic fallback runs (and can itself
    reject). A watch is only ever produced for an explicit request.

    ``watches`` enables natural CORRECTIONS (stop/forget/snooze/exclude/change):
    a correction is resolved against the named/unique active watch; ambiguity or
    no match fails closed and is never turned into a new broad watch.
    """
    raw = str(text or "")
    if not raw.strip() or is_acknowledgement(raw):
        raise IntakeError("message is not an explicit watch request")
    if len(raw) > MAX_REQUEST_CHARS:
        raw = raw[:MAX_REQUEST_CHARS]
    if watches is not None:
        correction = _correction_action(raw, watches, provenance=provenance)
        if correction is not None:
            return correction
    if not looks_like_explicit_request(raw):
        raise IntakeError("message is not an explicit watch request")
    if model is not None:
        try:
            proposal = model.extract(raw, policy)
            return validate_intake(proposal, provenance=provenance, now=now)
        except IntakeError:
            raise
        except Exception:  # noqa: BLE001 - any model failure falls back honestly
            pass
    return _fallback_action(raw, provenance=provenance, now=now)


class FakeIntakeModel:
    """Deterministic intake model for tests; never touches a network/model."""

    def __init__(self, proposal: Any):
        self._proposal = proposal
        self.calls: list[str] = []

    def extract(self, text: str, policy: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(text)
        result = self._proposal(text, policy) if callable(self._proposal) else self._proposal
        return json.loads(json.dumps(result))


class NativeIntakeModel:
    """Native Hermes zero-tool intake model with an injectable runner."""

    def __init__(self, hermes_bin: str = "hermes", profile: str = "default",
                 toolset: str = "context_engine",
                 runner: Callable[[list[str], str, int], tuple[int, str, str]] | None = None,
                 resolver: Callable[..., tuple[bool, list[str], str]] | None = None,
                 ctx: Any = None):
        self.hermes_bin = hermes_bin
        self.profile = profile
        self.toolset = toolset
        self.runner = runner
        self.resolver = resolver or resolve_toolset_tools
        # Optional host-built context: when usable, intake extraction goes through
        # the bounded host-owned ``ctx.llm`` lane; subprocess is the explicit
        # compatibility fallback for an older runtime.
        self.ctx = ctx
        self.route = "subprocess"
        self.calls = 0

    def _native_lane(self, policy: dict[str, Any]) -> Any:
        if self.ctx is None:
            return None
        from .native_runtime import build_model_lane

        lane = build_model_lane(self.ctx, policy, path="intake",
                                purpose="secretary.intake")
        if lane.available:
            return lane
        raise IntakeError(f"configured model lane unavailable: {lane.route.reason}")

    def build_prompt(self, text: str) -> str:
        return INTAKE_SYSTEM + "\n\nREQUEST:\n" + text + "\n\nReturn only the JSON object."

    def extract(self, text: str, policy: dict[str, Any]) -> dict[str, Any]:
        cfg = policy.get("evaluator", {}) or {}
        lane = self._native_lane(policy)
        if lane is not None:
            self.calls += 1
            try:
                self.route = "ctx.llm"
                return lane.complete_json(
                    instructions=self.build_prompt(text),
                    schema_name="secretary.intake",
                    max_tokens=int(policy.get("intake_max_tokens", 800)),
                    timeout=float(policy.get("intake_timeout_seconds", 60)),
                    temperature=0.0,
                )
            except Exception as exc:  # noqa: BLE001 - sanitized, caller falls back
                raise IntakeError(
                    f"intake ctx.llm failed ({type(exc).__name__})") from exc
        resolved_ok, names, detail = self.resolver(
            self.toolset, hermes_bin=self.hermes_bin,
            timeout=int(cfg.get("resolve_timeout_seconds", 30)))
        if not resolved_ok:
            raise IntakeError(f"toolset resolution failed: {detail}")
        if names:
            raise IntakeError(f"intake toolset exposes {len(names)} tools; refusing")
        prompt = self.build_prompt(text)
        argv = [
            self.hermes_bin, "-p", self.profile, "chat", "-Q",
            "--query-file", "-", "--source", "tool",
            "--run-budget", str(int(cfg.get("run_budget_seconds", 90))),
            "--max-turns", str(int(policy.get("intake_max_turns", 2))),
            "-t", self.toolset,
        ]
        timeout = int(policy.get("intake_timeout_seconds", 60))
        self.calls += 1
        if self.runner is not None:
            rc, out, err = self.runner(argv, prompt, timeout)
        else:
            try:
                proc = subprocess.run(argv, shell=False, input=prompt, text=True,
                                      capture_output=True, timeout=timeout, check=False)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                raise IntakeError("intake model invocation failed") from exc
            rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        if rc != 0:
            raise IntakeError("intake model failed")
        obj = _extract_json(out)
        if obj is None:
            raise IntakeError("intake model returned no parseable JSON")
        return obj


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def intake_command(args: Any) -> int:
    """``--intake TEXT`` / ``--intake-file PATH``: durable typed watch CLI."""
    import sys
    from pathlib import Path

    from .clock import Clock, SeededClock
    from .policy import load_policy
    from .state import StateStore
    from .watchstore import apply_action

    text = getattr(args, "intake", "") or ""
    path = getattr(args, "intake_file", "")
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"proactive-portal intake rejected: cannot read intake file: {exc}", file=sys.stderr)
            return 2
    clock = SeededClock(args.seeded_clock) if getattr(args, "seeded_clock", None) else Clock()
    now = clock.now()
    policy = load_policy(getattr(args, "policy", None))
    store = StateStore(Path(args.state_dir), clock)
    from .watchstore import active_watches, load_watchstore

    try:
        watches = active_watches(load_watchstore(store), now)
    except Exception:  # noqa: BLE001 - no active watch simply disables corrections
        watches = []
    model = (NativeIntakeModel(ctx=getattr(args, "native_ctx", None))
             if not getattr(args, "no_model", False) else None)
    # Explicit CLI provenance is preserved verbatim; an absent/empty value gets
    # the honest trusted-intake default. Never overwrite a supplied (e.g.
    # synthetic test) provenance with a claim of a genuine user request.
    provenance = _collapsed(getattr(args, "provenance", "")) or DEFAULT_INTAKE_PROVENANCE
    try:
        action = intake_to_action(text, policy=policy, model=model,
                                  provenance=provenance, now=now, watches=watches)
    except IntakeError as exc:
        print(f"proactive-portal intake rejected: {exc}", file=sys.stderr)
        return 2
    try:
        result = apply_action(store, action, now)
    except WatchError as exc:
        print(f"proactive-portal intake rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "intake": "saved",
        "id": result.get("id"),
        "version": result.get("version"),
        "public_topic": result.get("public_topic"),
        "scope": result.get("scope"),
        "provenance": result.get("provenance"),
    }, indent=2))
    return 0
