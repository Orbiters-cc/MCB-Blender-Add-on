import importlib


def get_xmuscle_api():
    try:
        return importlib.import_module("xmuscle_orbit_helper.api")
    except Exception:
        return None


def is_xmuscle_available(context):
    api = get_xmuscle_api()
    if api is None:
        return False
    try:
        return bool(api.is_available(context))
    except Exception:
        return False


def bake_for_export(context, body_obj, armature_obj, force_rebake=False):
    api = get_xmuscle_api()
    if api is None:
        return {
            "available": False,
            "baked": False,
            "warnings": ["XMuscle Orbit Helper API is not available"],
            "muscles": [],
        }
    return api.bake_for_mcb_export(
        context,
        body_obj=body_obj,
        rig_obj=armature_obj,
        force_rebake=force_rebake,
        create_preview_actions=True,
    )
