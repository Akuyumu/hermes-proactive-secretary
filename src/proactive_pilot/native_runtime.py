"""Native-first runtime bridge: plugin settings + the supported ``ctx.llm`` lane.

This module is the single place the ordinary pipeline talks to native Hermes.
It is intentionally duck-typed and never imports the Hermes runtime at module
import time, and it NEVER constructs ``PluginContext`` or ``PluginLlm`` itself:
the host-built context is passed in from the registered CLI handler closure or
from the scheduled runner, so normal discovery/consent and the per-plugin trust
gate stay authoritative.

Guarantees:

* Model calls go only through the host-owned ``ctx.llm`` facade and only after
  the registered configured-only guard allows this exact context. No provider/
  model/agent/profile override is ever requested, so no trust flag is needed and
  none can be widened. An unbound/foreign context is denied before dispatch.
* Every call is attributable: we pass a ``purpose`` and record which route was
  used (``ctx.llm`` vs ``unsupported``) without capturing raw prompts/secrets.
* The reminder dispatcher (and every known disabled/paused path) is
  model-free: building a route for those paths yields an unavailable route and
  makes zero calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Per-context dispatch guard. The plugin registration binds this AFTER deciding
# whether the host exposes a real configured-only contract. An unbound context is
# fail-closed: only the host-built context registered by this plugin may dispatch
# model work, and a wrong/foreign context cannot borrow another profile's route.
_MODEL_LANES: dict[int, dict[str, Any]] = {}
# Optional durable/clock pair bound by the production run for the SAME context.
# It lets the central LlmRoute boundary record truthful recovery/terminal
# failures without every caller threading a store into its planner/proposer.
_HEALTH_LANES: dict[int, dict[str, Any]] = {}

NATIVE_PLUGIN_ID = "hermes-proactive-secretary"
# Paths that must never spend a model call, ever.
MODEL_FREE_PATHS = frozenset({"reminder", "disabled", "paused", "public-only"})
# Plugin-owned auxiliary slot (registered in __init__.register). Routing it is
# the operator's native config (auxiliary.proactive_secretary); the plugin
# itself still never names a provider or model.
AUX_TASK_KEY = "proactive_secretary"


def _validated_aux_outcome(value: Any) -> dict[str, Any] | None:
    """Preserve only the documented strict-host observation attribute."""
    try:
        from .modelhealth import validate_aux_route_outcome
        return validate_aux_route_outcome(value)
    except Exception:  # noqa: BLE001 - malformed metadata is simply unavailable
        return None


def _aux_task_registered() -> bool:
    """True when the host registry holds this plugin's auxiliary slot."""
    try:
        from hermes_cli.plugins import get_plugin_auxiliary_tasks
        return any(e.get("key") == AUX_TASK_KEY for e in get_plugin_auxiliary_tasks())
    except Exception:  # noqa: BLE001 - no host registry: main-model path
        return False


class LlmUnavailable(RuntimeError):
    """No supported host-owned model route is available for this call."""


def bind_model_lane(ctx: Any, *, allowed: bool, reason: str = "",
                    status: dict[str, Any] | None = None) -> None:
    """Bind this exact host context's model-dispatch decision.

    Called only with the context the host passed to ``register(ctx)`` (or, in
    tests, an explicit fake). The context object itself is retained so an id
    reuse after garbage collection cannot inherit the decision.
    """
    if ctx is None:
        return
    _MODEL_LANES[id(ctx)] = {
        "ctx": ctx,
        "allowed": bool(allowed),
        "reason": str(reason or ""),
        "status": dict(status or {}),
    }


def bound_model_lane_status(ctx: Any) -> dict[str, Any] | None:
    """Return the sanitized registration status bound to this exact context."""
    entry = _MODEL_LANES.get(id(ctx)) if ctx is not None else None
    if entry is None or entry.get("ctx") is not ctx:
        return None
    return dict(entry.get("status") or {})


def bind_health_store(ctx: Any, store: Any, clock: Any = None) -> None:
    """Bind the run's health store for this exact context (best effort)."""
    if ctx is None or store is None:
        return
    _HEALTH_LANES[id(ctx)] = {"ctx": ctx, "store": store, "clock": clock}


def _health_for(ctx: Any) -> tuple[Any, Any] | None:
    entry = _HEALTH_LANES.get(id(ctx)) if ctx is not None else None
    if entry is None or entry.get("ctx") is not ctx:
        return None
    return entry.get("store"), entry.get("clock")


def model_lane_allowed(ctx: Any) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for this exact context; unbound is denied."""
    if ctx is None:
        return False, "no host context supplied"
    entry = _MODEL_LANES.get(id(ctx))
    if entry is None or entry.get("ctx") is not ctx:
        return False, ("portal model lane is not initialized for this context; "
                       "fail closed until plugin registration succeeds")
    return bool(entry.get("allowed")), str(entry.get("reason") or "")


def has_native_llm(ctx: Any) -> bool:
    """True when the injected context exposes the supported ``ctx.llm`` facade."""
    if ctx is None:
        return False
    try:
        llm = getattr(ctx, "llm", None)
    except Exception:  # noqa: BLE001 - a property error means "unavailable"
        return False
    return llm is not None and hasattr(llm, "complete_structured")


def read_native_settings(ctx: Any, key: str, default: Any = None) -> Any:
    """Read one plugin setting through the supported ``ctx.get_config`` accessor.

    Returns ``default`` when the context is absent or the read fails closed.
    """
    if ctx is None:
        return default
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return default
    try:
        return getter(key, default)
    except Exception:  # noqa: BLE001 - a rejected/failed read is "unset"
        return default


def native_capabilities(ctx: Any) -> dict[str, Any]:
    """Capability report for diagnostics; no secret or token is read."""
    return {
        "context": ctx is not None,
        "plugin_settings": ctx is not None and callable(getattr(ctx, "get_config", None)),
        "llm": has_native_llm(ctx),
    }


@dataclass
class LlmRoute:
    """A resolved, bounded, host-owned route (or an explicit unavailability)."""

    kind: str                      # "ctx.llm" | "unsupported"
    reason: str = ""
    ctx: Any = None
    purpose: str = "secretary"
    path: str = "evaluator"
    _calls: int = field(default=0, init=False, repr=False)

    @property
    def available(self) -> bool:
        if self.kind != "ctx.llm" or not has_native_llm(self.ctx):
            return False
        return model_lane_allowed(self.ctx)[0]

    @property
    def calls(self) -> int:
        return self._calls

    def describe(self) -> dict[str, Any]:
        """Route trace for status/diagnostics: no prompt or secret is included."""
        return {
            "route": self.kind,
            "purpose": self.purpose,
            "path": self.path,
            "available": self.available,
            "reason": self.reason,
            "model_calls": self._calls,
        }

    def complete_structured(
        self,
        *,
        instructions: str,
        input: list[dict[str, Any]],
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
    ) -> Any:
        """Run one bounded structured call through ``ctx.llm``.

        Never passes provider/model/agent_id/profile. Raises
        :class:`LlmUnavailable` (with a sanitized reason) when no route exists.
        """
        allowed, guard_reason = model_lane_allowed(self.ctx)
        if not self.available or not allowed:
            raise LlmUnavailable(guard_reason or self.reason or "no supported ctx.llm route")
        kwargs: dict[str, Any] = {
            "instructions": instructions,
            "input": input,
            "json_schema": json_schema,
            "json_mode": json_schema is None,
            "schema_name": schema_name,
            "system_prompt": system_prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "purpose": self.purpose,
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None or k in {
            "instructions", "input", "json_schema", "json_mode", "purpose"}}
        # Always name this plugin's own auxiliary task. A taskless call is the
        # host's main-model path, so an unresolved registration must reach the
        # host as a denied task, never as a call without one.
        kwargs["task"] = AUX_TASK_KEY
        self._calls += 1
        try:
            result = self.ctx.llm.complete_structured(**kwargs)
        except LlmUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitized, never leaks provider text
            error = LlmUnavailable(f"ctx.llm call failed ({type(exc).__name__})")
            outcome = _validated_aux_outcome(getattr(exc, "aux_route_outcome", None))
            if outcome is not None:
                setattr(error, "aux_route_outcome", outcome)
                self._consume_outcome(outcome)
            raise error from exc
        # The strict host emits no success schema. A returned result from the
        # SAME configured route is still a genuine success (including a fallback
        # success), so recovery is recorded at this central boundary instead of
        # being inferred from provider text.
        self._record_recovery()
        return result

    def _record_recovery(self) -> None:
        health = _health_for(self.ctx)
        if health is None:
            return
        store, clock = health
        try:
            from . import modelhealth
            modelhealth.recovery(store, role="route", now=clock.now() if clock else None)
        except Exception:  # noqa: BLE001 - health bookkeeping never fails a call
            return

    def _consume_outcome(self, outcome: dict[str, Any]) -> None:
        health = _health_for(self.ctx)
        if health is None:
            return
        store, clock = health
        try:
            from . import modelhealth
            modelhealth.consume_terminal_outcome(
                store, outcome, now=clock.now() if clock else None)
        except Exception:  # noqa: BLE001 - health bookkeeping never masks the call
            return


def build_llm_route(
    ctx: Any,
    policy: dict[str, Any] | None = None,
    *,
    path: str = "evaluator",
    purpose: str = "secretary",
) -> LlmRoute:
    """Resolve the model route for one pipeline path, fail-closed.

    ``path`` in :data:`MODEL_FREE_PATHS` always yields an unavailable route, so a
    reminder/disabled/paused path can never spend a model call even if a context
    is mistakenly supplied.
    """
    if path in MODEL_FREE_PATHS:
        return LlmRoute(kind="unsupported", ctx=None, path=path, purpose=purpose,
                        reason=f"model-free path: {path}")
    if not has_native_llm(ctx):
        return LlmRoute(kind="unsupported", ctx=None, path=path, purpose=purpose,
                        reason="host context does not expose ctx.llm"
                        if ctx is not None else "no host context supplied")
    allowed, guard_reason = model_lane_allowed(ctx)
    if not allowed:
        return LlmRoute(kind="unsupported", ctx=None, path=path, purpose=purpose,
                        reason=guard_reason)
    return LlmRoute(kind="ctx.llm", ctx=ctx, path=path, purpose=purpose,
                    reason="host-owned configured route")


def evaluator_route(ctx: Any, policy: dict[str, Any] | None = None) -> LlmRoute:
    """Route for the ordinary evaluator/planner/proposer path."""
    return build_llm_route(ctx, policy, path="evaluator", purpose="secretary.evaluate")


def reminder_route(policy: dict[str, Any] | None = None) -> LlmRoute:
    """The reminder dispatcher is deterministic and model-free by construction."""
    return build_llm_route(None, policy, path="reminder", purpose="secretary.reminder")


@dataclass
class NativeModelLane:
    """A reusable, bounded JSON extraction lane over one ``ctx.llm`` route.

    Every model lane (evaluator, planner, inferred-interest proposer, semantic
    privacy verifier, conversational intake) uses this single wrapper, so the
    host-owned trust boundary, purpose attribution and sanitized failure
    behaviour are identical everywhere. It never requests a provider/model/agent/
    profile override and never sees a credential.
    """

    route: LlmRoute

    @property
    def available(self) -> bool:
        return self.route.available

    def describe(self) -> dict[str, Any]:
        return self.route.describe()

    def complete_json(
        self,
        *,
        instructions: str,
        system_prompt: str | None = None,
        schema_name: str | None = None,
        purpose: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Run one bounded structured call and return the parsed JSON object."""
        if not self.available:
            raise LlmUnavailable(self.route.reason or "no supported ctx.llm route")
        from .evaluator import _extract_json

        result = self.route.complete_structured(
            instructions=instructions,
            input=[{"type": "text", "text": "Return only the JSON object."}],
            schema_name=schema_name,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            timeout=timeout,
            temperature=temperature,
        )
        parsed = getattr(result, "parsed", None)
        text = getattr(result, "text", "") or ""
        obj = parsed if isinstance(parsed, dict) else _extract_json(text)
        if obj is None:
            raise LlmUnavailable("ctx.llm returned no parseable JSON object")
        return obj


def build_model_lane(
    ctx: Any,
    policy: dict[str, Any] | None = None,
    *,
    path: str = "model",
    purpose: str = "secretary.model",
) -> NativeModelLane:
    """Resolve a reusable JSON lane for one pipeline path (fail-closed)."""
    return NativeModelLane(build_llm_route(ctx, policy, path=path, purpose=purpose))


__all__ = [
    "NATIVE_PLUGIN_ID", "MODEL_FREE_PATHS", "LlmUnavailable", "LlmRoute",
    "NativeModelLane", "has_native_llm", "read_native_settings", "native_capabilities",
    "bind_model_lane", "model_lane_allowed", "bound_model_lane_status",
    "build_llm_route", "build_model_lane", "evaluator_route", "reminder_route",
    "bind_health_store",
]
