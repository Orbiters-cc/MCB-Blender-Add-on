import bpy
import hashlib
import os
from pathlib import Path
import bpy.utils.previews as previews

from .sync import get_pending_sync_offer, get_sync_session
from .exporter import dirty_mesh_count, dirty_mesh_name_set, export_mesh_report
from . import xmuscle_bridge

_PREVIEWS = None


def _asset_path(name):
    return str(Path(__file__).resolve().parent / "assets" / name)


def register_previews():
    global _PREVIEWS
    if _PREVIEWS is None:
        _PREVIEWS = previews.new()


def unregister_previews():
    global _PREVIEWS
    if _PREVIEWS is not None:
        previews.remove(_PREVIEWS)
        _PREVIEWS = None


def _existing_path(path, fallback=None):
    if path and os.path.exists(path):
        return path
    if fallback and os.path.exists(fallback):
        return fallback
    return ""


def _preview_icon_id(key, path):
    if _PREVIEWS is None or not path or not os.path.exists(path):
        return 0

    try:
        stat = os.stat(path)
    except OSError:
        return 0

    cache_key_source = key + "|" + os.path.abspath(path) + "|" + str(stat.st_mtime_ns)
    cache_key = hashlib.sha1(cache_key_source.encode("utf-8")).hexdigest()
    if cache_key not in _PREVIEWS:
        _PREVIEWS.load(cache_key, path, "IMAGE")
    return _PREVIEWS[cache_key].icon_id


def _draw_icon_image(layout, key, path, scale):
    icon_id = _preview_icon_id(key, path)
    if icon_id:
        row = layout.row()
        row.alignment = "CENTER"
        row.template_icon(icon_value=icon_id, scale=scale)
        return True
    return False


def _connection_state(session, pending_offer):
    if session:
        return "connected", "Connected to Unity MCB", _asset_path("dot_connected.png")
    if pending_offer:
        return "waiting", "Waiting for Unity MCB", _asset_path("dot_waiting.png")
    return "disconnected", "Disconnected", _asset_path("dot_disconnected.png")


def _draw_connection_status(layout, session, pending_offer):
    _state, label, dot_path = _connection_state(session, pending_offer)
    row = layout.row(align=True)
    icon_id = _preview_icon_id("sync_dot", dot_path)
    if icon_id:
        row.template_icon(icon_value=icon_id, scale=0.45)
        row.label(text=label)
    else:
        icon = "LINKED" if session else ("TIME" if pending_offer else "UNLINKED")
        row.label(text=label, icon=icon)


def _draw_brand_header(layout, settings):
    banner_path = _existing_path(settings.ui_banner_path, _asset_path("banner.png"))
    if banner_path:
        _draw_icon_image(layout, "banner", banner_path, 4.0)

    if settings.ui_user_name or settings.ui_avatar_path:
        row = layout.row(align=True)
        avatar_path = _existing_path(settings.ui_avatar_path)
        icon_id = _preview_icon_id("avatar", avatar_path)
        if icon_id:
            row.template_icon(icon_value=icon_id, scale=2.2)
        else:
            row.label(text="", icon="USER")
        row.label(text=settings.ui_user_name or "Unity user")
        layout.separator()


class MCB_PT_panel(bpy.types.Panel):
    bl_label = "MCB"
    bl_idname = "MCB_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCB"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mcb_blender
        session = get_sync_session(settings)
        pending_offer = get_pending_sync_offer(settings)

        _draw_brand_header(layout, settings)

        col = layout.column(align=True)
        col.label(text="Magic Sync")
        _draw_connection_status(col, session, pending_offer)
        row = col.row(align=True)
        row.operator("mcb.smart_magic_sync", text="Sync with Unity MCB", icon="LINKED")
        row.operator("mcb.clear_magic_sync", text="", icon="X")

        if session:
            col.label(text="Unity: " + (settings.unity_project_path or "Unknown"), icon="LINKED")
            col.label(text="Custom Base: " + (settings.custom_base_name or "Custom Base"), icon="OUTLINER_OB_ARMATURE")
            if settings.blender_project_path:
                col.label(text="Project: " + Path(settings.blender_project_path).name, icon="FILE_BLEND")
            if settings.unity_exports_path:
                col.label(text="Exports: " + settings.unity_exports_path, icon="FILE_FOLDER")
        elif pending_offer:
            wait_row = col.row(align=True)
            wait_row.operator("mcb.check_magic_sync_response", text="", icon="FILE_REFRESH")
        else:
            col.label(text="Start here or click Sync with Blender in Unity", icon="INFO")

        col.separator()
        col.label(text="Export")
        save_row = col.row(align=True)
        save_row.prop(settings, "sync_on_save")
        if settings.sync_on_save:
            save_row.label(text=str(dirty_mesh_count(settings, context.scene)) + " dirty", icon="FILE_TICK")
        col.prop(settings, "armature_object")
        if session:
            report = export_mesh_report(context, session)
            mesh_names = report["meshes"]
            missing_names = report["missing"]
            dirty_names = dirty_mesh_name_set(settings, context.scene)
            if mesh_names:
                col.label(text="Detected meshes (" + str(len(mesh_names)) + ")", icon="MESH_DATA")
                mesh_box = col.box()
                for mesh_name in mesh_names:
                    mesh_row = mesh_box.row(align=True)
                    mesh_row.label(text=mesh_name, icon="MESH_DATA")
                    if mesh_name in dirty_names:
                        mesh_row.label(text="dirty", icon="FILE_TICK")
                    sync_op = mesh_row.operator("mcb.send_to_unity", text="Sync", icon="EXPORT")
                    sync_op.requested_mesh_name = mesh_name
            else:
                col.label(text="No matching Blender meshes detected", icon="ERROR")
            if missing_names:
                col.label(text=str(len(missing_names)) + " Unity target mesh name(s) missing", icon="INFO")

        xmuscle_api = xmuscle_bridge.get_xmuscle_api()
        xrow = col.row(align=True)
        xrow.prop(settings, "include_xmuscle")
        xrow.enabled = xmuscle_api is not None
        if settings.include_xmuscle and xmuscle_api is None:
            col.label(text=f"XMuscle Orbit Helper (API {xmuscle_bridge.SUPPORTED_TOOLKIT_API_VERSION}) is not enabled", icon="INFO")
        elif settings.include_xmuscle:
            col.prop(settings, "xmuscle_force_rebake")
            if not xmuscle_api.is_available():
                col.label(text="X-Muscle System is off: only earlier bakes are sent", icon="INFO")

        col.separator()
        button_text = "Sync with Unity"
        if settings.custom_base_name:
            button_text += " (" + settings.custom_base_name + ")"
        send_row = col.row()
        send_row.enabled = session is not None
        send_row.operator("mcb.send_to_unity", text=button_text, icon="EXPORT")

        if settings.last_status:
            col.separator()
            col.label(text=settings.last_status, icon="INFO")
