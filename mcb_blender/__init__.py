import bpy
from bpy.props import PointerProperty

from .properties import MCB_Settings
from .sync import (
    MCB_OT_check_magic_sync_response,
    MCB_OT_clear_magic_sync,
    MCB_OT_connect_magic_sync,
    MCB_OT_smart_magic_sync,
    MCB_OT_start_magic_sync,
    register_heartbeat_timer,
    unregister_heartbeat_timer,
)
from .exporter import MCB_OT_send_to_unity, register_save_handlers, unregister_save_handlers
from .ui import MCB_PT_panel, register_previews, unregister_previews

ALL_CLASSES = (
    MCB_Settings,
    MCB_OT_smart_magic_sync,
    MCB_OT_start_magic_sync,
    MCB_OT_check_magic_sync_response,
    MCB_OT_connect_magic_sync,
    MCB_OT_clear_magic_sync,
    MCB_OT_send_to_unity,
    MCB_PT_panel,
)


def register():
    for cls in ALL_CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mcb_blender = PointerProperty(type=MCB_Settings)
    register_previews()
    register_heartbeat_timer()
    register_save_handlers()


def unregister():
    unregister_save_handlers()
    unregister_heartbeat_timer()
    unregister_previews()
    if hasattr(bpy.types.Scene, "mcb_blender"):
        del bpy.types.Scene.mcb_blender
    for cls in reversed(ALL_CLASSES):
        bpy.utils.unregister_class(cls)
