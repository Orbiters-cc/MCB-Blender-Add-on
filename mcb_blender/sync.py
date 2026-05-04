import json
import os
import tempfile
import uuid
import datetime
from pathlib import Path

import bpy

MAGIC_SYNC_KIND = "orbiters.mcb.magicSync"
MAGIC_SYNC_OFFER_KIND = "orbiters.mcb.blenderMagicSyncOffer"
HEARTBEAT_KIND = "orbiters.mcb.blenderHeartbeat"
PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL_SECONDS = 1.0


def get_settings(context):
    scene = getattr(context, "scene", None)
    return getattr(scene, "mcb_blender", None) if scene else None


def get_sync_session(settings):
    if settings is None or not settings.sync_session_json:
        return None
    try:
        data = json.loads(settings.sync_session_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("kind") != MAGIC_SYNC_KIND:
        return None
    return data


def apply_sync_session(settings, data):
    settings.sync_session_json = json.dumps(data, sort_keys=True)
    settings.unity_project_path = data.get("unityProjectPath", "") or ""
    settings.unity_inbox_path = data.get("inboxPath", "") or ""
    settings.heartbeat_path = data.get("heartbeatPath", "") or ""
    project = data.get("blenderProject") if isinstance(data.get("blenderProject"), dict) else {}
    settings.blender_project_path = project.get("projectAbsolutePath", "") or project.get("absolutePath", "") or ""
    settings.unity_exports_path = project.get("exportsUnityPath", "") or project.get("exportsAbsolutePath", "") or ""
    ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
    user = ui.get("user") if isinstance(ui.get("user"), dict) else {}
    settings.ui_banner_path = ui.get("bannerPath", "") or ""
    settings.ui_avatar_path = user.get("avatarPath", "") or ""
    settings.ui_user_name = user.get("name", "") or ""
    custom_base = data.get("selectedCustomBase") or {}
    settings.custom_base_name = custom_base.get("name", "") or data.get("customBaseName", "") or "Custom Base"
    settings.pending_sync_offer_json = ""
    settings.pending_sync_response_path = ""
    settings.last_status = "Connected to Unity MCB: " + settings.custom_base_name
    write_heartbeat(settings)


def write_heartbeat(settings):
    session = get_sync_session(settings)
    heartbeat_path = settings.heartbeat_path if settings else ""
    if not session or not heartbeat_path:
        return False

    path = Path(heartbeat_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "kind": HEARTBEAT_KIND,
                    "protocolVersion": PROTOCOL_VERSION,
                    "sessionId": session.get("sessionId", ""),
                    "token": session.get("token", ""),
                    "updatedAtUtc": datetime.datetime.utcnow().isoformat() + "Z",
                    "blendFile": bpy.data.filepath,
                },
                handle,
                indent=2,
                sort_keys=True,
            )
        os.replace(temp_path, path)
        return True
    except OSError as exc:
        settings.last_status = "Failed to write Blender heartbeat: " + str(exc)
        return False


def _heartbeat_timer():
    for scene in bpy.data.scenes:
        settings = getattr(scene, "mcb_blender", None)
        if settings is not None:
            poll_pending_sync_response(settings)
            write_heartbeat(settings)
    return HEARTBEAT_INTERVAL_SECONDS


def register_heartbeat_timer():
    if not bpy.app.timers.is_registered(_heartbeat_timer):
        bpy.app.timers.register(_heartbeat_timer, persistent=True)


def unregister_heartbeat_timer():
    if bpy.app.timers.is_registered(_heartbeat_timer):
        bpy.app.timers.unregister(_heartbeat_timer)


def get_pending_sync_offer(settings):
    if settings is None or not settings.pending_sync_offer_json:
        return None
    try:
        data = json.loads(settings.pending_sync_offer_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("kind") != MAGIC_SYNC_OFFER_KIND:
        return None
    return data


def poll_pending_sync_response(settings):
    offer = get_pending_sync_offer(settings)
    response_path = settings.pending_sync_response_path if settings else ""
    if not offer or not response_path or not os.path.exists(response_path):
        return False

    try:
        with open(response_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        settings.last_status = "Failed to read pending Unity sync response: " + str(exc)
        return False

    if data.get("kind") != MAGIC_SYNC_KIND:
        settings.last_status = "Pending Unity response is not an MCB Magic Sync payload"
        return False
    if int(data.get("protocolVersion", 0)) > PROTOCOL_VERSION:
        settings.last_status = "Unity uses a newer MCB sync protocol"
        return False
    if data.get("blenderOfferSessionId") != offer.get("sessionId") or data.get("blenderOfferToken") != offer.get("token"):
        settings.last_status = "Pending Unity response does not match this Blender sync offer"
        return False

    apply_sync_session(settings, data)
    return True


class MCB_OT_start_magic_sync(bpy.types.Operator):
    bl_idname = "mcb.start_magic_sync"
    bl_label = "Start Magic Sync"
    bl_description = "Copy a Blender Magic Sync offer so Unity MCB can connect back to this Blender project"

    def execute(self, context):
        settings = get_settings(context)
        if settings is None:
            self.report({"ERROR"}, "MCB settings are unavailable")
            return {"CANCELLED"}

        session_id = uuid.uuid4().hex
        token = uuid.uuid4().hex
        response_dir = Path(tempfile.gettempdir()) / "orbiters_mcb_blender_sync" / session_id
        response_dir.mkdir(parents=True, exist_ok=True)
        response_path = response_dir / "unity_response.json"

        offer = {
            "kind": MAGIC_SYNC_OFFER_KIND,
            "protocolVersion": PROTOCOL_VERSION,
            "sessionId": session_id,
            "token": token,
            "responsePath": str(response_path),
            "blenderBlendFile": bpy.data.filepath,
            "blenderAddon": "MCB",
            "blenderAddonVersion": "0.1.0",
        }

        settings.pending_sync_offer_json = json.dumps(offer, sort_keys=True)
        settings.pending_sync_response_path = str(response_path)
        settings.last_status = "Blender Magic Sync copied. Click Sync with Blender in Unity."
        context.window_manager.clipboard = json.dumps(offer, indent=2, sort_keys=True)
        self.report({"INFO"}, settings.last_status)
        return {"FINISHED"}


def _try_connect_from_clipboard(context, settings):
    raw = context.window_manager.clipboard.strip()
    if not raw:
        return False, "Clipboard is empty"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, "Clipboard does not contain valid Magic Sync JSON: " + str(exc)

    if data.get("kind") != MAGIC_SYNC_KIND:
        return False, "Clipboard JSON is not an MCB Magic Sync payload"

    if int(data.get("protocolVersion", 0)) > PROTOCOL_VERSION:
        return False, "Unity uses a newer MCB sync protocol"

    if not data.get("inboxPath") or not data.get("sessionId") or not data.get("token"):
        return False, "Magic Sync payload is missing inbox/session data"

    apply_sync_session(settings, data)
    return True, settings.last_status


def _start_sync_offer(context, settings):
    session_id = uuid.uuid4().hex
    token = uuid.uuid4().hex
    response_dir = Path(tempfile.gettempdir()) / "orbiters_mcb_blender_sync" / session_id
    response_dir.mkdir(parents=True, exist_ok=True)
    response_path = response_dir / "unity_response.json"

    offer = {
        "kind": MAGIC_SYNC_OFFER_KIND,
        "protocolVersion": PROTOCOL_VERSION,
        "sessionId": session_id,
        "token": token,
        "responsePath": str(response_path),
        "blenderBlendFile": bpy.data.filepath,
        "blenderAddon": "MCB",
        "blenderAddonVersion": "0.1.0",
    }

    settings.pending_sync_offer_json = json.dumps(offer, sort_keys=True)
    settings.pending_sync_response_path = str(response_path)
    settings.last_status = "Blender Magic Sync copied. Click Sync with Blender in Unity."
    context.window_manager.clipboard = json.dumps(offer, indent=2, sort_keys=True)
    return settings.last_status


class MCB_OT_smart_magic_sync(bpy.types.Operator):
    bl_idname = "mcb.smart_magic_sync"
    bl_label = "Sync with Unity MCB"
    bl_description = "Connect from a Unity MCB clipboard payload, or copy a Blender sync offer if none is available"

    def execute(self, context):
        settings = get_settings(context)
        if settings is None:
            self.report({"ERROR"}, "MCB settings are unavailable")
            return {"CANCELLED"}

        connected, message = _try_connect_from_clipboard(context, settings)
        if connected:
            self.report({"INFO"}, message)
            return {"FINISHED"}

        status = _start_sync_offer(context, settings)
        self.report({"INFO"}, status)
        return {"FINISHED"}


class MCB_OT_check_magic_sync_response(bpy.types.Operator):
    bl_idname = "mcb.check_magic_sync_response"
    bl_label = "Check Pending Sync"
    bl_description = "Check whether Unity has answered the pending Blender Magic Sync offer"

    def execute(self, context):
        settings = get_settings(context)
        if settings is None:
            return {"CANCELLED"}
        if poll_pending_sync_response(settings):
            self.report({"INFO"}, settings.last_status)
            return {"FINISHED"}
        self.report({"INFO"}, settings.last_status or "Still waiting for Unity MCB")
        return {"CANCELLED"}


class MCB_OT_connect_magic_sync(bpy.types.Operator):
    bl_idname = "mcb.connect_magic_sync"
    bl_label = "Connect Magic Sync From Clipboard"
    bl_description = "Read Unity MCB Magic Sync data from the clipboard"

    def execute(self, context):
        settings = get_settings(context)
        if settings is None:
            self.report({"ERROR"}, "MCB settings are unavailable")
            return {"CANCELLED"}

        connected, message = _try_connect_from_clipboard(context, settings)
        if not connected:
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        self.report({"INFO"}, settings.last_status)
        return {"FINISHED"}


class MCB_OT_clear_magic_sync(bpy.types.Operator):
    bl_idname = "mcb.clear_magic_sync"
    bl_label = "Clear Magic Sync"
    bl_description = "Forget the current Unity Magic Sync session"

    def execute(self, context):
        settings = get_settings(context)
        if settings is None:
            return {"CANCELLED"}
        settings.sync_session_json = ""
        settings.pending_sync_offer_json = ""
        settings.pending_sync_response_path = ""
        settings.unity_project_path = ""
        settings.unity_inbox_path = ""
        settings.heartbeat_path = ""
        settings.blender_project_path = ""
        settings.unity_exports_path = ""
        settings.ui_banner_path = ""
        settings.ui_avatar_path = ""
        settings.ui_user_name = ""
        settings.custom_base_name = ""
        settings.last_status = "Magic Sync cleared"
        return {"FINISHED"}
