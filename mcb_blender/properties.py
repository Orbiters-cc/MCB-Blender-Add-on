import time

import bpy
from bpy.props import BoolProperty, FloatProperty, PointerProperty, StringProperty


def _body_poll(_self, obj):
    return obj and obj.type == "MESH" and not getattr(obj, "Muscle_XID", False)


def _armature_poll(_self, obj):
    return obj and obj.type == "ARMATURE"


def _sync_on_save_update(self, _context):
    self.dirty_mesh_names_json = ""
    self.dirty_mesh_signatures_json = ""
    self.sync_on_save_ignore_until = time.time() + 1.5
    self.last_status = "Sync on Save enabled" if self.sync_on_save else "Sync on Save disabled"


class MCB_Settings(bpy.types.PropertyGroup):
    body_object: PointerProperty(
        name="Body",
        type=bpy.types.Object,
        description="Mesh exported to Unity as the custom base body",
        poll=_body_poll,
    )
    armature_object: PointerProperty(
        name="Armature",
        type=bpy.types.Object,
        description="Armature exported with the custom base body",
        poll=_armature_poll,
    )
    include_xmuscle: BoolProperty(
        name="Include XMuscle deformation",
        description="Ask XMuscle Orbit Helper to bake muscle deformation before exporting, when available",
        default=True,
    )
    xmuscle_force_rebake: BoolProperty(
        name="Force muscle rebake",
        description="Rebake muscle shape keys even when previous baked keys exist",
        default=False,
    )
    sync_on_save: BoolProperty(
        name="Sync on Save",
        description="Automatically sync modified MCB meshes to Unity after saving the Blender file",
        default=False,
        update=_sync_on_save_update,
    )
    dirty_mesh_names_json: StringProperty(default="")
    dirty_mesh_signatures_json: StringProperty(default="")
    sync_on_save_ignore_until: FloatProperty(default=0.0)
    sync_session_json: StringProperty(default="")
    pending_sync_offer_json: StringProperty(default="")
    pending_sync_response_path: StringProperty(default="")
    unity_project_path: StringProperty(name="Unity Project", default="")
    unity_inbox_path: StringProperty(name="Unity Inbox", default="")
    heartbeat_path: StringProperty(name="Heartbeat Path", default="")
    blender_project_path: StringProperty(name="Blender Project", default="")
    unity_exports_path: StringProperty(name="Unity Export Folder", default="")
    ui_banner_path: StringProperty(name="Banner Path", default="")
    ui_avatar_path: StringProperty(name="Avatar Path", default="")
    ui_user_name: StringProperty(name="Unity User", default="")
    custom_base_name: StringProperty(name="Custom Base", default="")
    last_status: StringProperty(default="")
