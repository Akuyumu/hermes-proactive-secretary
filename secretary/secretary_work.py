"""Optional plugin-owned preparation tools (NOT the recommended background path).

The corrected integration uses the existing no-agent/script bridge plus one
bounded structured draft through the native auxiliary route and the
deterministic writer. These tools are retained only as a narrow optional
surface where the host already owns registration/approval; do not use a native
cron AIAgent with prompt-only toolset restrictions as a sandbox. This module is
not imported by the existing root ``register(ctx)`` in this candidate.

The tools are deliberately narrow:

* they operate only on a responsibility that the deterministic preflight has
  already claimed (token + generation + source-version fence);
* there is no path/URL/command argument and no generic shell/write/read tool;
* the only write is one bounded Markdown/text artifact beneath the owner-only
  plugin artifact root;
* they cannot complete a responsibility, send, publish, book, purchase or mutate
  tasks/calendars/user documents.

Prompts alone are not a sandbox; the host must also scope the owned job toolset
to exactly ``proactive_preparation`` with MCP disabled.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from proactive_pilot import ownerpaths as _ownerpaths  # noqa: E402

TOOLSET = "proactive_preparation"
PLUGIN_NAME = "hermes-proactive-secretary"
DATA_ENV = "HERMES_PROACTIVE_SECRETARY_DATA"
READ_TOOL = "preparation_read_brief"
WRITE_TOOL = "preparation_write_artifact"

READ_SCHEMA: dict[str, Any] = {
    "name": READ_TOOL,
    "description": (
        "Read the bounded, explicitly-untrusted data packet for the one responsibility "
        "claimed by this preparation run. Requires the opaque responsibility id and the "
        "one-run claim token from the preflight context. Reads no private source and "
        "accepts no path."),
    "parameters": {
        "type": "object",
        "properties": {
            "responsibility_id": {"type": "string", "description": "Opaque preflight id."},
            "claim_token": {"type": "string", "description": "One-run claim token."},
        },
        "required": ["responsibility_id", "claim_token"],
        "additionalProperties": False,
    },
}

WRITE_SCHEMA: dict[str, Any] = {
    "name": WRITE_TOOL,
    "description": (
        "Create one private Markdown/text preparation artifact for the current claimed "
        "responsibility beneath the plugin-owned artifact root. Content is treated as data; "
        "citations must use supplied evidence refs and supplied URLs only. This never "
        "completes the responsibility and performs no external effect."),
    "parameters": {
        "type": "object",
        "properties": {
            "responsibility_id": {"type": "string", "description": "Opaque preflight id."},
            "claim_token": {"type": "string", "description": "One-run claim token."},
            "generation": {"type": "integer", "description": "Current claim generation."},
            "source_version": {"type": "string", "description": "Current source version."},
            "title": {"type": "string", "description": "Short inspectable artifact title."},
            "content": {"type": "string", "description": "Bounded Markdown/text body."},
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Supplied/current evidence refs cited by the artifact.",
            },
            "format": {"type": "string", "enum": ["markdown", "text"]},
        },
        "required": [
            "responsibility_id", "claim_token", "generation", "source_version",
            "title", "content", "evidence_refs",
        ],
        "additionalProperties": False,
    },
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _data_root() -> Path:
    """Profile-safe data root shared with every other owner surface."""
    return _ownerpaths.data_root()


def _store() -> Any:
    from proactive_pilot.clock import Clock
    from proactive_pilot.state import StateStore

    return StateStore(_data_root() / "var", Clock())


def _gate(gate_fn: Callable[[], Any] | None) -> None:
    from proactive_pilot.preparation import check_gate

    check_gate(gate_fn)


def _required_str(args: dict[str, Any], name: str, limit: int = 4096) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    if len(value) > limit:
        raise ValueError(f"{name} is too long")
    return value.strip()


def read_brief(args: dict, *, gate_fn: Callable[[], Any] | None = None,
               **kwargs: Any) -> str:
    """Return only the current claim holder's bounded packet."""
    try:
        from proactive_pilot.responsibilities import claimed_brief

        _gate(gate_fn)
        rid = _required_str(args, "responsibility_id", 96)
        token = _required_str(args, "claim_token", 128)
        packet = claimed_brief(_store(), rid, token)
        return _json({"ok": True, "brief": packet})
    except Exception as exc:  # noqa: BLE001 - tools always return bounded JSON
        return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})


def write_artifact(args: dict, *, gate_fn: Callable[[], Any] | None = None,
                   **kwargs: Any) -> str:
    """Create/verify/queue one owned private artifact without external effects."""
    try:
        from proactive_pilot.preparation import write_artifact as _write

        _gate(gate_fn)
        rid = _required_str(args, "responsibility_id", 96)
        token = _required_str(args, "claim_token", 128)
        source_version = args.get("source_version")
        if not isinstance(source_version, str):
            raise ValueError("source_version must be a string")
        generation = args.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise ValueError("generation must be an integer")
        title = _required_str(args, "title", 100)
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be text")
        evidence_refs = args.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not all(
                isinstance(ref, str) for ref in evidence_refs):
            raise ValueError("evidence_refs must be a list of strings")
        fmt = args.get("format") or "markdown"
        if fmt not in {"markdown", "text"}:
            raise ValueError("format must be markdown or text")
        result = _write(
            _store(),
            responsibility_id=rid,
            claim_token=token,
            generation=generation,
            source_version=source_version,
            title=title,
            content=content,
            evidence_refs=evidence_refs,
            fmt=fmt,
            gate_fn=gate_fn,
        )
        return _json({"ok": True, "artifact": result})
    except Exception as exc:  # noqa: BLE001 - tools always return bounded JSON
        return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]})


def register_tools(ctx: Any, *, gate_provider: Callable[[], Any] | None = None) -> None:
    """Register the two plugin-owned tools with the real host signature.

    ``gate_provider`` is a trusted integrator callback (no arguments) that
    re-evaluates the authoritative enable/pause/scope/consent gate. If it is
    absent or raises, both handlers fail closed. There is no ambient config read
    and no model-supplied gate/path. No override, generic, or optional tool is
    registered automatically.
    """
    def _read(args: dict, **kwargs: Any) -> str:
        return read_brief(args, gate_fn=gate_provider, **kwargs)

    def _write(args: dict, **kwargs: Any) -> str:
        return write_artifact(args, gate_fn=gate_provider, **kwargs)

    ctx.register_tool(
        name=READ_TOOL,
        toolset=TOOLSET,
        schema=READ_SCHEMA,
        handler=_read,
        description="Read the bounded private-preparation brief for the claimed unit.",
    )
    ctx.register_tool(
        name=WRITE_TOOL,
        toolset=TOOLSET,
        schema=WRITE_SCHEMA,
        handler=_write,
        description="Create one verified private Markdown/text preparation artifact.",
    )


__all__ = [
    "TOOLSET", "READ_TOOL", "WRITE_TOOL", "READ_SCHEMA", "WRITE_SCHEMA",
    "read_brief", "write_artifact", "register_tools",
]
