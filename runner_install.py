"""Install and own the native-cron runner scripts.

The native scheduler runs ``.py`` jobs from the profile's scripts directory
(``$HERMES_HOME/scripts``). The plugin ships two default templates plus an
explicit opt-in preparation runner under ``runners/``;
:func:`install_runners` substitutes the installed plugin root into each and
writes it into that directory, recording ownership so only files this plugin
created are ever removed.

Rules:
- Idempotent: identical content is a no-op.
- An unrelated pre-existing file of the same name is backed up before it is
  overwritten, never silently clobbered.
- Removal only touches files this plugin recorded AND whose content still
  matches the recorded digest; a user-modified file is kept and reported.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNNER_FILENAMES = (
    "proactive_secretary_runner.py",
    "proactive_secretary_reminder_runner.py",
)
PREPARATION_RUNNER_FILENAMES = (
    "proactive_secretary_prepare_runner.py",
)
PLACEHOLDER = "{{PLUGIN_ROOT}}"
BACKUP_SUFFIX = ".pre-hermes-proactive-secretary.bak"
PLUGIN_NAME = "hermes-proactive-secretary"
SCHEMA = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _load_ownership(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    files = data.get("files")
    if not isinstance(files, dict):
        files = {}
    return {"schema": SCHEMA, "plugin": PLUGIN_NAME, "files": files,
            **{k: v for k, v in data.items() if k not in ("schema", "plugin", "files")}}


def _save_ownership(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with _suppress_oserror():
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class _suppress_oserror:
    def __enter__(self) -> "_suppress_oserror":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return exc[0] is OSError


def _backup_path(target: Path) -> Path:
    candidate = target.with_name(target.name + BACKUP_SUFFIX)
    counter = 2
    while candidate.exists():
        candidate = target.with_name(f"{target.name}{BACKUP_SUFFIX}.{counter}")
        counter += 1
    return candidate


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    with _suppress_oserror():
        os.chmod(tmp, 0o700)
    os.replace(tmp, target)


def install_runners(*, plugin_root: Path, template_root: Path, scripts_dir: Path,
                    ownership_path: Path, now: str | None = None,
                    include_preparation: bool = False) -> list[dict[str, Any]]:
    """Install owned runner templates into ``scripts_dir`` and record ownership.

    The preparation runner is installed only when the caller explicitly opts in
    (``hermes secretary setup --with-preparation``); merely setting
    ``preparation_enabled`` in configuration never installs or arms it.
    """
    installed_at = now or _now()
    ownership = _load_ownership(ownership_path)
    results: list[dict[str, Any]] = []
    names = RUNNER_FILENAMES + (
        PREPARATION_RUNNER_FILENAMES if include_preparation else ())
    for name in names:
        source = template_root / name
        try:
            text = source.read_text(encoding="utf-8").replace(PLACEHOLDER, str(plugin_root))
        except OSError as exc:
            raise FileNotFoundError(f"shipped runner template missing: {source}") from exc
        digest = _sha256_text(text)
        target = scripts_dir / name
        record = ownership["files"].get(str(target))
        action = "installed"
        backup: str | None = None
        if target.exists():
            current = target.read_text(encoding="utf-8", errors="replace")
            if current == text:
                action = "unchanged"
            elif record and str(record.get("sha256")) == _sha256_text(current):
                action = "updated"
            else:
                backup_path = _backup_path(target)
                backup_path.write_bytes(target.read_bytes())
                with _suppress_oserror():
                    os.chmod(backup_path, 0o600)
                backup = str(backup_path)
                action = "backed-up"
        if action != "unchanged":
            _atomic_write(target, text)
        ownership["files"][str(target)] = {
            "sha256": digest,
            "source": name,
            "installed_at": installed_at,
            **({"backup": backup} if backup else {}),
        }
        entry: dict[str, Any] = {"file": str(target), "action": action}
        if backup:
            entry["backup"] = backup
        results.append(entry)
    _save_ownership(ownership_path, ownership)
    return results


def uninstall_runners(*, ownership_path: Path, scripts_dir: Path | None = None) -> list[dict[str, Any]]:
    """Remove every runner file this plugin recorded and still owns."""
    ownership = _load_ownership(ownership_path)
    results: list[dict[str, Any]] = []
    for path_str, record in list(ownership["files"].items()):
        path = Path(path_str)
        if scripts_dir is not None and path.parent != scripts_dir:
            continue
        if not path.exists():
            results.append({"file": path_str, "action": "missing"})
            ownership["files"].pop(path_str, None)
            continue
        if str(record.get("sha256") or "") == _sha256_file(path):
            try:
                path.unlink()
            except OSError as exc:
                results.append({"file": path_str, "action": "kept", "detail": f"unlink failed: {exc}"})
                continue
            results.append({"file": path_str, "action": "removed"})
            ownership["files"].pop(path_str, None)
        else:
            results.append({"file": path_str, "action": "kept",
                            "detail": "modified since install; not removed"})
    _save_ownership(ownership_path, ownership)
    return results


def owned_files(ownership_path: Path) -> list[str]:
    return sorted(_load_ownership(ownership_path)["files"].keys())
