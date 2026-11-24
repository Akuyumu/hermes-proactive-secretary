"""Supported Dashboard/Desktop backend for the proactive secretary settings page.

Mounted by Hermes at ``/api/plugins/hermes-proactive-secretary/``. The desktop
half reaches these routes only through ``ctx.rest('/setup/...')`` — the
namespace-scoped, host-authenticated plugin REST door. No shell, arbitrary file
or remote fetch route exists here.

Routes:

``GET  /setup/status``          bounded status for the settings page
``GET  /settings``              ordinary editable values + revision
``POST /settings/preview``      prospective changes and permission expansion (no write)
``POST /settings``              explicit typed patch through the config owner
``POST /setup/trigger``         non-destructive foreground continuation
``POST /setup/reset/prepare``   one-time challenge + exact reset scope
``POST /setup/reset/commit``    owner-confirmed reset after private backup

Authentication is the host's plugin-route authentication. Cross-site browser
requests are refused, and a reset needs the unguessable challenge issued by
``prepare`` plus the exact typed phrase. A wrong, expired or stale challenge
writes nothing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from secretary.secretary_setup_actions import (  # noqa: E402
    SetupActionError, SetupActions, owner_data_root, owner_profile_home,
)

router = APIRouter()
PLUGIN_ID = "hermes-proactive-secretary"


def _same_path(left: Any, right: Any) -> bool:
    def key(value: Any) -> str:
        try:
            return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))
        except (OSError, RuntimeError, TypeError):
            return os.path.normcase(os.path.abspath(os.path.expanduser(str(value))))
    return key(left) == key(right)


def _owner_plugin_enabled() -> bool:
    """Whether THIS owner profile enables the plugin backend.

    The dashboard's launch-scope gate is not enough when one process serves
    several profiles: a request scoped to profile B must not borrow profile
    A's enablement. Read the ACTIVE profile's own allow/deny lists, and fail
    closed on an unreadable setting.
    """
    try:
        from hermes_cli.plugins_cmd import _get_disabled_set, _get_enabled_set
        enabled, disabled = _get_enabled_set(), _get_disabled_set()
    except Exception:  # noqa: BLE001 - fail closed, never borrow another profile
        return False
    return PLUGIN_ID in enabled and PLUGIN_ID not in disabled


def _load_owner_package(manager: Any, manifest: Any) -> str:
    """Resolve the agent package for THIS plugin directory and THIS profile.

    The real host loader keys directory modules per profile (a suffix when
    another profile already claimed the bare name). Look up only the name the
    current manager would use; never borrow a same-file module that belongs to
    another profile. If that module was loaded earlier in this scope, reuse it;
    otherwise ask the loader, which computes the scope-safe name.
    """
    init_file = (_ROOT / "__init__.py").resolve()
    namer = getattr(manager, "_directory_module_name", None)
    expected = namer(manifest) if callable(namer) else ""
    if expected:
        module = sys.modules.get(expected)
        filename = getattr(module, "__file__", None) if module is not None else None
        if (filename and _same_path(filename, init_file)
                and getattr(module, "__path__", None)):
            return getattr(module, "__name__", expected)
    elif not callable(getattr(manager, "_load_directory_module", None)):
        # Test/embedding doubles without the host loader: only an exact-file
        # module from this directory may be reused.
        for module in list(sys.modules.values()):
            filename = getattr(module, "__file__", None)
            if not filename or not _same_path(filename, init_file):
                continue
            package = getattr(module, "__name__", "")
            if package and getattr(module, "__path__", None):
                return package

    loader = getattr(manager, "_load_directory_module", None)
    if not callable(loader):
        return ""
    try:
        module = (loader(manifest, module_name=expected) if expected
                  else loader(manifest))
    except Exception as exc:  # noqa: BLE001 - services degrade; config stays owner-bound
        raise SetupActionError(
            "plugin_module_unavailable",
            f"The installed plugin module could not be loaded ({type(exc).__name__}).",
            status_code=503) from exc
    filename = getattr(module, "__file__", None)
    if not filename or not _same_path(Path(filename).parent, _ROOT):
        raise SetupActionError(
            "plugin_identity_mismatch",
            "The loaded plugin module does not belong to this installed directory.",
            status_code=503)
    package = getattr(module, "__name__", "")
    if not package:
        raise SetupActionError("plugin_module_unavailable",
                               "The installed plugin module has no import name.", status_code=503)
    return package


def _plugin_context() -> tuple[Any, str]:
    """Build the plugin context for THIS request's active owner profile.

    The host's profile-scope dependency has already selected the request's
    home (``?profile=`` or the launch profile). The manager and context must
    belong to that same home; a manager for another profile is refused rather
    than silently used. Prefer the already-loaded agent plugin, then load the
    real package through the host loader, then parse this plugin's manifest.
    """
    from hermes_cli.plugins import PluginContext, get_plugin_manager

    manager = get_plugin_manager()
    manager_home = getattr(manager, "home_path", None)
    if manager_home is not None:
        if not _same_path(manager_home, owner_profile_home()):
            raise SetupActionError(
                "profile_scope_mismatch",
                "The plugin manager does not belong to the requested profile.",
                status_code=503)

    loaded = None
    for key, candidate in getattr(manager, "_plugins", {}).items():
        manifest = getattr(candidate, "manifest", None)
        if getattr(manifest, "name", "") == PLUGIN_ID or key == PLUGIN_ID:
            loaded = candidate
            break
    if loaded is not None:
        if not bool(getattr(loaded, "enabled", True)):
            raise SetupActionError(
                "plugin_not_enabled",
                "This plugin is not enabled in the requested profile.",
                status_code=503)
        manifest = loaded.manifest
        manifest_path = getattr(manifest, "path", None)
        module = getattr(loaded, "module", None)
        module_file = getattr(module, "__file__", None)
        if manifest_path and not _same_path(manifest_path, _ROOT):
            raise SetupActionError(
                "plugin_identity_mismatch",
                "The loaded plugin manifest does not belong to this installed directory.",
                status_code=503)
        if module_file and not _same_path(Path(module_file).parent, _ROOT):
            raise SetupActionError(
                "plugin_identity_mismatch",
                "The loaded plugin module does not belong to this installed directory.",
                status_code=503)
        package = getattr(module, "__name__", "")
        return PluginContext(manifest, manager), package

    if manager_home is not None and not _owner_plugin_enabled():
        raise SetupActionError(
            "plugin_not_enabled",
            "This plugin is not enabled in the requested profile.",
            status_code=503)

    from hermes_cli.plugins_manifest import parse_manifest_file

    manifest = parse_manifest_file(_ROOT / "plugin.yaml", _ROOT, "user", "")
    if manifest is None:
        raise SetupActionError("plugin_manifest_unavailable",
                               "The installed plugin manifest could not be read.", status_code=503)
    return PluginContext(manifest, manager), _load_owner_package(manager, manifest)


def _foundation_backup_writer(ctx: Any, data_root: Any = None):
    """Adapter to the shared owner-only config backup helper when installed.

    The helper (``proactive_pilot.config_backup``) snapshots exactly this
    plugin's config/preferences/onboarding/native settings with hashes. Reset
    keeps its own fallback for older source trees, but production should use
    the one shared helper rather than grow a second backup format.
    """
    try:
        from proactive_pilot import config_backup
    except Exception:
        return None

    def writer(*, loaded: Any, onboarding: Any, preferences: Any, reason: str):
        result = config_backup.create_backup(
            Path(data_root) if data_root is not None else owner_data_root(),
            native=ctx, state=getattr(ctx, "state", None), reason=reason,
        )
        files = {str(entry.get("name")): {"bytes": entry.get("bytes"),
                                          "sha256": entry.get("sha256")}
                 for entry in (result.get("files") or []) if isinstance(entry, dict)}
        return {"id": Path(result["path"]).name, "path": result["path"],
                "files": files, "manifest_sha256": result.get("manifest_sha256", "")}

    return writer


def _service() -> SetupActions:
    ctx, package = _plugin_context()
    return SetupActions(ctx, package_name=package,
                        backup_writer=_foundation_backup_writer(ctx))


def _guard(request: Request) -> None:
    """Reject cross-site browser traffic. Native bridge calls have no Origin."""
    if str(request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
        raise SetupActionError("cross_site_refused", "Cross-site setup actions are not allowed.",
                               status_code=403)
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host") or ""
        if urlsplit(origin).netloc.lower() != host.lower():
            raise SetupActionError("origin_refused", "Cross-origin setup actions are not allowed.",
                                   status_code=403)


def _error_response(exc: SetupActionError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code,
                        content={"ok": False, "error": exc.code, "message": str(exc)})


def _unavailable(exc: Exception) -> JSONResponse:
    # Never leak a path, value or traceback into the UI.
    return JSONResponse(status_code=503,
                        content={"ok": False, "error": "setup_unavailable",
                                 "message": f"Setup actions are unavailable ({type(exc).__name__})."})


@router.get("/setup/status")
def setup_status(request: Request):
    try:
        _guard(request)
        return _service().status()
    except SetupActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001 - plugin contract: bounded error, never raise out
        return _unavailable(exc)


@router.get("/settings")
def settings_read(request: Request):
    try:
        _guard(request)
        return _service().settings()
    except SetupActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        from proactive_pilot.userconfig import ConfigError
        if isinstance(exc, ConfigError):
            return JSONResponse(status_code=409,
                                content={"ok": False, "error": "configuration_invalid",
                                         "message": "Existing configuration is invalid. Fix it in "
                                                    "Agent settings before editing here."})
        return _unavailable(exc)


@router.post("/settings/preview")
def settings_preview(body: dict[str, Any], request: Request):
    try:
        _guard(request)
        if not isinstance(body, dict):
            raise SetupActionError("invalid_body", "A JSON object is required.")
        values = body.get("values")
        if not isinstance(values, dict):
            raise SetupActionError("invalid_body", "A settings object is required.")
        return _service().preview_settings(values=values, revision=body.get("revision"))
    except SetupActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        from proactive_pilot.userconfig import ConfigError
        if isinstance(exc, ConfigError):
            return JSONResponse(status_code=409,
                                content={"ok": False, "error": "configuration_invalid",
                                         "message": "Existing configuration is invalid. Fix it in "
                                                    "Agent settings before editing here."})
        return _unavailable(exc)


@router.post("/settings")
def settings_update(body: dict[str, Any], request: Request):
    try:
        _guard(request)
        if not isinstance(body, dict):
            raise SetupActionError("invalid_body", "A JSON object is required.")
        values = body.get("values")
        if not isinstance(values, dict):
            raise SetupActionError("invalid_body", "A settings object is required.")
        return _service().update_settings(
            values=values, revision=body.get("revision"),
            acknowledged_expanding=body.get("acknowledged_expanding"))
    except SetupActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        from proactive_pilot.userconfig import ConfigError
        if isinstance(exc, ConfigError):
            return JSONResponse(status_code=409,
                                content={"ok": False, "error": "configuration_invalid",
                                         "message": "Existing configuration is invalid. Fix it in "
                                                    "Agent settings before editing here."})
        return _unavailable(exc)


@router.get("/cadence")
def cadence_read(request: Request):
    """Read-only: stored interval, whether the live Portal is gated, next due."""
    try:
        _guard(request)
        from secretary import secretary_cadence as cadence
        from secretary import secretary_cli as secretary_cli
        view = cadence.effective(secretary_cli._backend())
        view["status"] = "gated" if view.get("gated") else "not-gated"
        return view
    except SetupActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001 - bounded error, never raise out
        return _unavailable(exc)


@router.post("/cadence")
def cadence_write(body: dict[str, Any], request: Request):
    """Store the interval, and (only with ``apply``) convert the Portal job.

    ``apply`` refuses with nothing changed when the job is paused, duplicated or
    holding scheduled/retry state — the states a native schedule rewrite would
    destroy. Without ``apply`` the live job is untouched.
    """
    try:
        _guard(request)
        if not isinstance(body, dict):
            raise SetupActionError("invalid_body", "A JSON object is required.")
        interval = body.get("interval")
        if interval in (None, ""):
            raise SetupActionError("invalid_body", "An interval is required.")
        apply_now = bool(body.get("apply"))
        from secretary import secretary_cadence as cadence
        if apply_now:
            from secretary import secretary_cli as secretary_cli
            result = cadence.convert(secretary_cli._backend(), interval)
        else:
            plan = cadence.write_plan(interval)
            result = {**plan, "status": "stored",
                      "detail": "stored only; the live Portal schedule is unchanged"}
        return JSONResponse(status_code=200 if result.get("ok") else 409, content=result)
    except SetupActionError as exc:
        return _error_response(exc)
    except ValueError as exc:
        return JSONResponse(status_code=409,
                            content={"ok": False, "status": "refused", "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - bounded error, never raise out
        return _unavailable(exc)


@router.post("/setup/trigger")
def setup_trigger(request: Request):
    try:
        _guard(request)
        return _service().trigger()
    except SetupActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(exc)


@router.post("/setup/reset/prepare")
def setup_reset_prepare(request: Request):
    try:
        _guard(request)
        return _service().prepare_reset()
    except SetupActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        from proactive_pilot.userconfig import ConfigError
        from proactive_pilot.feedback import FeedbackError
        if isinstance(exc, (ConfigError, FeedbackError)):
            return JSONResponse(status_code=409,
                                content={"ok": False, "error": "configuration_invalid",
                                         "message": "Existing configuration is invalid. Fix it in "
                                                    "Agent settings before resetting."})
        return _unavailable(exc)


@router.post("/setup/reset/commit")
def setup_reset_commit(body: dict[str, Any], request: Request):
    try:
        _guard(request)
        if not isinstance(body, dict):
            raise SetupActionError("invalid_body", "A JSON object is required.")
        return _service().commit_reset(
            challenge=body.get("challenge"),
            phrase=body.get("phrase"),
            explicit=body.get("explicit"),
        )
    except SetupActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        from proactive_pilot.userconfig import ConfigError
        from proactive_pilot.feedback import FeedbackError
        if isinstance(exc, (ConfigError, FeedbackError)):
            return JSONResponse(status_code=409,
                                content={"ok": False, "error": "configuration_invalid",
                                         "message": "Existing configuration is invalid. Fix it in "
                                                    "Agent settings before resetting."})
        return _unavailable(exc)
