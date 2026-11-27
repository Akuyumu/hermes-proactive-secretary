"""Bounded owner-facing notice prose and the deterministic voice check.

This module owns the notice rendering previously embedded in
:mod:`proactive_pilot.preparation` and the narrow pre-send voice contract used by
the portal proposal transport. It performs no I/O and never calls a model.
"""
from __future__ import annotations

import re
from typing import Any

_CITATION = re.compile(r"\[\[\s*ref\s*:\s*([^\]\n]{1,240})\s*\]\]", re.IGNORECASE)
_REF_TOKEN = re.compile(
    r"\b(?:mail|calendar|task|tasks|document|doc|conversation|memory|feed):"
    r"[A-Za-z0-9._:-]{1,200}", re.IGNORECASE)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>)\]}'\"]+", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_NOTICE_ARTIFACT = re.compile(
    r"\b(?:artifact|responsibility|generation|ref|path|handle)\s*[:=]\s*\S+",
    re.IGNORECASE)
_NOTICE_PATH = re.compile(r"(?:/[\w.~-]+){2,}")
_NOTICE_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._:-]{23,}\b")
# Opaque internal handles are frequently short (resp_ab12cd34, artifact:…,
# sec-…). They are stripped explicitly so a bare id cannot survive prose.
_NOTICE_OPAQUE_ID = re.compile(
    r"\b(?:resp|artifact|artifact_ref|sec|cal|mail|task|thread|evt|msg|"
    r"responsibility|handle)_[A-Za-z0-9][A-Za-z0-9_-]{3,}\b",
    re.IGNORECASE)
_NOTICE_HEX = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
_NOTICE_SPACE = re.compile(r"\s+")

_LONG_MESSAGE = 480
_HEX_BLOB_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_HASH_TOKEN_RE = re.compile(
    r"\b(?:md5|sha1|sha256)\b\s*(?:[:=]\s*)?"
    r"(?=[A-Za-z0-9+/=_-]*\d)[A-Za-z0-9+/=_-]{8,}\b",
    re.IGNORECASE)
_OPAQUE_ID_RE = re.compile(
    r"\b0x[0-9a-fA-F]{6,}\b|"
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", re.IGNORECASE)
# Any path-shaped token, not a list of directory names: URLs are stripped
# before this runs, and the lookbehind keeps "and/or", "24/7" and "s://" out.
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.~-])/[\w.~-]+"
    r"|(?<![\w:/])[A-Za-z]:[\\/][\w.\\/-]+")
# The plugin's own record ids are shapes a reader can never use; a bare prefix
# such as resp_ab12cd34 or fu_9f2c1a is an internal handle even at 11 chars.
_PLUGIN_ID_RE = re.compile(
    r"\b(?:fu|resp|sec|cal|mail|task|thread|evt|msg|artifact|handle|responsibility|"
    r"reminder|followup|obs|cand)_[A-Za-z0-9]{4,}\b", re.IGNORECASE)
_LEADING_TIMESTAMP_RE = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?\b")
_SENTENCE_END_RE = re.compile(r"[.?!]")


def _notice_fragment(value: Any, limit: int) -> str:
    """Bounded, identifier-free owner prose for chat.

    Only accepted responsibility prose is used, never an artifact title/path,
    ref, generation counter or source identifier. Untrusted text cannot inject
    a ref/URL/path-shaped handle into the rendered sentence.
    """
    text = str(value or "")
    text = _CITATION.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _REF_TOKEN.sub(" ", text)
    text = _NOTICE_ARTIFACT.sub(" ", text)
    text = _NOTICE_PATH.sub(" ", text)
    text = _NOTICE_LONG_TOKEN.sub(" ", text)
    text = _NOTICE_OPAQUE_ID.sub(" ", text)
    text = _NOTICE_HEX.sub(" ", text)
    text = _CONTROL.sub(" ", text)
    text = text.replace("<", " ").replace(">", " ")
    text = _NOTICE_SPACE.sub(" ", text).strip(" \"'“”‘’.,;:-")
    if len(text) > limit:
        text = text[:limit].rstrip(" ,;:-") + "..."
    return text


def render_ready_notice(rec: dict[str, Any]) -> str:
    """Task-specific, bounded notice or ``""`` when there is nothing useful.

    The first ready notice is not mandatory: a generic status-only sentence is
    never emitted. The owner's own goal and acceptance note provide the useful
    task-specific content; detailed linkage stays structured/on demand.
    """
    goal = _notice_fragment(rec.get("desired_outcome"), 180)
    if not goal:
        return ""
    criterion = _notice_fragment(rec.get("acceptance_criterion"), 120)
    text = f'Your private draft for "{goal}" is ready to review.'
    if criterion and criterion.casefold() not in goal.casefold():
        text += f' It was prepared against your acceptance note: "{criterion}".'
    text += " Open it from your preparation queue when convenient."
    return text


def check_notice_text(text: str) -> tuple[bool, str | None]:
    """Return ``(True, None)`` or ``(False, rule_id)`` for one notice message.

    The rules are deliberately narrow and deterministic. They are a last-line
    shape check, not a truth or action classifier.
    """
    value = str(text or "")
    if len(value) > _LONG_MESSAGE:
        return False, "too_long"
    if _HASH_TOKEN_RE.search(value):
        return False, "hash_token"
    if _HEX_BLOB_RE.search(value):
        return False, "hex_blob"
    if _OPAQUE_ID_RE.search(value):
        return False, "opaque_id"
    if _PLUGIN_ID_RE.search(value):
        return False, "plugin_id"
    # URLs are legitimate notice content ("join at https://…"), so remove them
    # before looking for filesystem paths — the drive-letter branch otherwise
    # matches the "s://" tail of every https link.
    if _ABSOLUTE_PATH_RE.search(_URL.sub(" ", value)):
        return False, "absolute_path"
    if _LEADING_TIMESTAMP_RE.search(value):
        return False, "leading_timestamp"
    if not _SENTENCE_END_RE.search(value):
        return False, "no_sentence_end"
    return True, None
