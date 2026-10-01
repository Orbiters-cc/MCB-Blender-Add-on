import importlib
import time
import traceback

import addon_utils
import bpy

XMUSCLES_EXTENSION_ID = "xmusclesystem"
TOOLKIT_ADDON_ID = "xmuscle_orbit_helper"
SUPPORTED_TOOLKIT_API_VERSION = 1
STATUS_REFRESH_SECONDS = 5.0

_status_cache = {"time": 0.0, "value": None}


def _is_addon(module_name, addon_id):
    # Extensions load as bl_ext.<repository>.<id>.
    return module_name == addon_id or module_name.endswith("." + addon_id)


def _enabled_module_name(addon_id):
    for module_name in bpy.context.preferences.addons.keys():
        if _is_addon(module_name, addon_id):
            return module_name
    return ""


def _addon_status(addon_id):
    enabled_name = _enabled_module_name(addon_id)
    for module in addon_utils.modules(refresh=False):
        if _is_addon(module.__name__, addon_id):
            version = addon_utils.module_bl_info(module).get("version", ())
            return {
                "installed": True,
                "enabled": module.__name__ == enabled_name,
                "version": ".".join(str(part) for part in version),
            }
    return {"installed": bool(enabled_name), "enabled": bool(enabled_name), "version": ""}


def environment_status():
    """Install state of the X-Muscle System and XMuscle Orbit Helper, reported in the Unity heartbeat."""
    now = time.monotonic()
    if _status_cache["value"] is None or now - _status_cache["time"] >= STATUS_REFRESH_SECONDS:
        _status_cache["value"] = {
            "xmuscles": _addon_status(XMUSCLES_EXTENSION_ID),
            "xmuscleToolkit": _addon_status(TOOLKIT_ADDON_ID),
        }
        _status_cache["time"] = now
    return _status_cache["value"]


def get_xmuscle_api():
    """The api module of the enabled XMuscle Orbit Helper when it has the supported API, else None."""
    module_name = _enabled_module_name(TOOLKIT_ADDON_ID)
    if not module_name:
        return None
    try:
        api = importlib.import_module(module_name + ".api")
    except ImportError:
        return None
    return api if getattr(api, "API_VERSION", 0) == SUPPORTED_TOOLKIT_API_VERSION else None


def bake_for_export(context, mesh_objects, armature_obj, force_rebake=False):
    """The XMuscle correctives of mesh_objects for manifest.xmuscle, or None without XMuscle Orbit Helper.

    Merges the toolkit's bake_for_mcb_export results of every mesh:
    {"apiVersion": 1, "muscles": [...], "warnings": [...]}. Muscles of meshes without X-Muscles
    are simply absent; a failed bake becomes a warning.
    """
    api = get_xmuscle_api()
    if api is None:
        return None

    result = {"apiVersion": api.API_VERSION, "muscles": [], "warnings": []}
    for mesh_obj in mesh_objects:
        try:
            # Preview actions would pose the rig, and the FBX export writes the current pose.
            mesh_result = api.bake_for_mcb_export(
                context,
                mesh_obj,
                armature_obj,
                force_rebake=force_rebake,
                create_preview_actions=False,
            )
        except Exception as exc:
            traceback.print_exc()
            result["warnings"].append(f"{mesh_obj.name}: XMuscle bake failed: {exc}")
            continue
        result["muscles"].extend(mesh_result.get("muscles") or [])
        for warning in mesh_result.get("warnings") or []:
            if warning not in result["warnings"]:
                result["warnings"].append(warning)
    return result
