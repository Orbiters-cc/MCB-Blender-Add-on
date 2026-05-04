import contextlib
import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import StringProperty

from .sync import get_settings, get_sync_session
from . import xmuscle_bridge

EXPORT_KIND = "orbiters.mcb.blenderExport"
READY_KIND = "orbiters.mcb.blenderExportReady"
PROTOCOL_VERSION = 1
SAVE_SYNC_DELAY_SECONDS = 1.5
TARGET_FBX_PROP = "mcb_target_fbx_path"
TARGET_MESH_PROP = "mcb_target_mesh_name"
TARGET_RENDERER_PROP = "mcb_target_renderer_name"

_IS_EXPORTING = False
_SAVE_SYNC_TIMER_PENDING = False


def _normalize_unity_path(path):
    return (path or "").replace("\\", "/").lower()


def _target_path_matches(left, right):
    return bool(left and right and _normalize_unity_path(left) == _normalize_unity_path(right))


def _mesh_tag_value(obj, key):
    value = obj.get(key, "") if obj is not None else ""
    return value if isinstance(value, str) else ""


def _mesh_tag_target_path(obj):
    return _mesh_tag_value(obj, TARGET_FBX_PROP)


def _find_default_body(context):
    obj = context.scene.objects.get("Body")
    if obj and obj.type == "MESH" and not getattr(obj, "Muscle_XID", False):
        return obj
    for candidate in context.scene.objects:
        if candidate.type == "MESH" and not getattr(candidate, "Muscle_XID", False):
            return candidate
    return None


def _find_armature_for_body(context, body_obj):
    if body_obj is not None:
        for modifier in body_obj.modifiers:
            if modifier.type == "ARMATURE" and getattr(modifier, "object", None):
                return modifier.object
        if body_obj.parent and body_obj.parent.type == "ARMATURE":
            return body_obj.parent
    obj = context.scene.objects.get("Armature")
    if obj and obj.type == "ARMATURE":
        return obj
    for candidate in context.scene.objects:
        if candidate.type == "ARMATURE":
            return candidate
    return None


def _shape_key_names(mesh_obj):
    keys = getattr(mesh_obj.data, "shape_keys", None) if mesh_obj else None
    if keys is None:
        return []
    return [key.name for key in keys.key_blocks if key.name != "Basis"]


def _shape_keys_by_mesh(mesh_objects):
    return {obj.name: _shape_key_names(obj) for obj in mesh_objects}


def _armature_bone_names(armature_obj):
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return []
    return [bone.name for bone in armature_obj.data.bones]


@contextlib.contextmanager
def _preserved_selection(context):
    active = context.view_layer.objects.active
    selected = [obj for obj in context.selected_objects]
    view_layer_objects = list(context.view_layer.objects)
    try:
        yield
    finally:
        for obj in view_layer_objects:
            obj.select_set(False)
        for obj in selected:
            if context.view_layer.objects.get(obj.name) == obj:
                obj.select_set(True)
        if active and context.view_layer.objects.get(active.name) == active:
            context.view_layer.objects.active = active


@contextlib.contextmanager
def _objects_in_view_layer(context, objects):
    collection = None
    linked = []
    hidden_state = {}
    hide_select_state = {}
    try:
        missing = [obj for obj in objects if obj is not None and context.view_layer.objects.get(obj.name) != obj]
        if missing:
            collection = bpy.data.collections.new("MCB Export View Layer")
            context.scene.collection.children.link(collection)
            for obj in missing:
                collection.objects.link(obj)
                linked.append(obj)
            context.view_layer.update()

        for obj in objects:
            if obj is None or context.view_layer.objects.get(obj.name) != obj:
                continue
            hide_select_state[obj.name] = obj.hide_select
            hidden_state[obj.name] = obj.hide_get()
            obj.hide_select = False
            obj.hide_set(False)

        yield
    finally:
        for obj in objects:
            if obj is None:
                continue
            if obj.name in hide_select_state:
                obj.hide_select = hide_select_state[obj.name]
            if obj.name in hidden_state and context.view_layer.objects.get(obj.name) == obj:
                obj.hide_set(hidden_state[obj.name])
        if collection is not None:
            for obj in linked:
                if collection.objects.get(obj.name) == obj:
                    collection.objects.unlink(obj)
            context.scene.collection.children.unlink(collection)
            bpy.data.collections.remove(collection)
            context.view_layer.update()


def _target_smr_entries(session):
    entries = []
    for target in session.get("targetFbxFiles") or []:
        if not isinstance(target, dict):
            continue
        target_path = target.get("unityPath") or target.get("path") or ""
        for entry in target.get("smrPaths") or []:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item["targetFbxPath"] = target_path
            entries.append(item)
    return entries


def _target_files(session):
    files = []
    for index, target in enumerate(session.get("targetFbxFiles") or []):
        if not isinstance(target, dict):
            continue
        path = target.get("unityPath") or target.get("path") or ""
        if not path:
            continue
        files.append({"index": index, "path": path, "data": target})
    return files


def _target_mesh_names(session):
    names = []
    seen = set()
    for target in _target_files(session):
        for value in _target_declared_mesh_names(target):
            if value and value not in seen:
                names.append(value)
                seen.add(value)
    for entry in _target_smr_entries(session):
        for value in _target_entry_names(entry):
            if value and value not in seen:
                names.append(value)
                seen.add(value)
    return names


def _target_entry_names(entry):
    return [
        entry.get("meshName"),
        entry.get("rendererName"),
        Path(entry.get("fbxMeshPath") or "").name,
    ]


def _target_declared_mesh_names(target):
    data = target.get("data") if isinstance(target, dict) else target
    if not isinstance(data, dict):
        return []
    values = data.get("meshNames") or data.get("fbxMeshNames") or []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def _strip_blender_suffix(name):
    if not name:
        return ""
    return re.sub(r"\.\d{3}$", "", name)


def _mesh_candidate_names(obj):
    names = []
    for value in (
        _mesh_tag_value(obj, TARGET_MESH_PROP),
        _mesh_tag_value(obj, TARGET_RENDERER_PROP),
        getattr(obj, "name", ""),
        getattr(getattr(obj, "data", None), "name", ""),
    ):
        if not value:
            continue
        names.append(value)
        stripped = _strip_blender_suffix(value)
        if stripped and stripped != value:
            names.append(stripped)
    return names


def _target_known_names(target):
    names = []
    names.extend(_target_declared_mesh_names(target))
    data = target.get("data") if isinstance(target, dict) else target
    if isinstance(data, dict):
        for entry in data.get("smrPaths") or []:
            if not isinstance(entry, dict):
                continue
            for value in _target_entry_names(entry):
                if value:
                    names.append(value)
    return names


def _tagged_meshes_for_target(context, target):
    path = target.get("path") if isinstance(target, dict) else ""
    if not path:
        return []

    names = set(_target_known_names(target))
    tagged = []
    for obj in context.scene.objects:
        if obj.type != "MESH" or getattr(obj, "Muscle_XID", False):
            continue
        if not _target_path_matches(_mesh_tag_target_path(obj), path):
            continue
        if names and not names.intersection(_mesh_candidate_names(obj)):
            continue
        tagged.append(obj)
    return tagged


def _scene_mesh_lookup(context):
    lookup = {}
    for obj in context.scene.objects:
        if obj.type != "MESH" or getattr(obj, "Muscle_XID", False):
            continue
        for name in _mesh_candidate_names(obj):
            lookup.setdefault(name, obj)
    return lookup


def find_export_meshes(context, session):
    meshes = []
    missing = []
    seen = set()
    target_files = _target_files(session)

    for target in target_files:
        for obj in _tagged_meshes_for_target(context, target):
            if obj.name not in seen:
                meshes.append(obj)
                seen.add(obj.name)

    if meshes:
        return meshes, missing

    lookup = _scene_mesh_lookup(context)
    declared_names = []
    for target in target_files:
        declared_names.extend(_target_declared_mesh_names(target))

    if declared_names:
        for name in declared_names:
            obj = lookup.get(name) or lookup.get(_strip_blender_suffix(name))
            if obj and obj.type == "MESH" and not getattr(obj, "Muscle_XID", False):
                if obj.name not in seen:
                    meshes.append(obj)
                    seen.add(obj.name)
            elif name:
                missing.append(name)
    else:
        for entry in _target_smr_entries(session):
            names = [name for name in _target_entry_names(entry) if name]
            obj = None
            for name in names:
                obj = lookup.get(name) or lookup.get(_strip_blender_suffix(name))
                if obj is not None:
                    break
            if obj and obj.type == "MESH" and not getattr(obj, "Muscle_XID", False):
                if obj.name not in seen:
                    meshes.append(obj)
                    seen.add(obj.name)
            else:
                missing_name = entry.get("meshName") or entry.get("rendererName") or Path(entry.get("fbxMeshPath") or "").name
                if missing_name:
                    missing.append(missing_name)

    if meshes:
        return meshes, missing

    body = _find_default_body(context)
    return ([body] if body else []), missing


def _safe_file_stem(value):
    stem = Path(value or "custom_base").stem or "custom_base"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)


def _mesh_groups_by_target(session, mesh_objects):
    target_files = _target_files(session)
    if not target_files:
        return [{"targetFbxPath": _target_fbx_path(session, [obj.name for obj in mesh_objects]), "meshes": mesh_objects}]

    by_name = {}
    for obj in mesh_objects:
        for name in _mesh_candidate_names(obj):
            by_name.setdefault(name, obj)
    assigned = set()
    groups = []

    for target in target_files:
        tagged = [
            obj
            for obj in mesh_objects
            if obj.name not in assigned and _target_path_matches(_mesh_tag_target_path(obj), target["path"])
        ]
        if tagged:
            groups.append({"targetFbxPath": target["path"], "meshes": tagged})
            assigned.update(obj.name for obj in tagged)
            continue

        names = _target_declared_mesh_names(target)
        if not names:
            names = []
            for entry in target["data"].get("smrPaths") or []:
                if not isinstance(entry, dict):
                    continue
                for value in _target_entry_names(entry):
                    if value:
                        names.append(value)
        meshes = []
        for name in names:
            obj = by_name.get(name) or by_name.get(_strip_blender_suffix(name))
            if obj is not None and obj.name not in assigned:
                meshes.append(obj)
                assigned.add(obj.name)
        if meshes:
            groups.append({"targetFbxPath": target["path"], "meshes": meshes})

    unassigned = [obj for obj in mesh_objects if obj.name not in assigned]
    if unassigned:
        if groups:
            groups[0]["meshes"].extend(unassigned)
        else:
            groups.append({"targetFbxPath": target_files[0]["path"], "meshes": unassigned})

    return groups


def _target_paths_for_meshes(session, mesh_objects):
    mesh_names = set()
    for obj in mesh_objects:
        mesh_names.update(_mesh_candidate_names(obj))

    target_paths = []
    seen = set()

    for obj in mesh_objects:
        tagged_path = _mesh_tag_target_path(obj)
        if tagged_path:
            for target in _target_files(session):
                if _target_path_matches(tagged_path, target["path"]) and target["path"] not in seen:
                    target_paths.append(target["path"])
                    seen.add(target["path"])

    if target_paths:
        return target_paths

    for target in _target_files(session):
        declared_names = set(_target_declared_mesh_names(target))
        if declared_names and mesh_names.intersection(declared_names):
            path = target["path"]
            if path and path not in seen:
                target_paths.append(path)
                seen.add(path)

    if target_paths:
        return target_paths

    for target in _target_files(session):
        for entry in target["data"].get("smrPaths") or []:
            if not isinstance(entry, dict):
                continue
            if any(name in mesh_names for name in _target_entry_names(entry) if name):
                path = target["path"]
                if path and path not in seen:
                    target_paths.append(path)
                    seen.add(path)
                break
    return target_paths


def _meshes_for_target_paths(session, all_mesh_objects, target_paths):
    if not target_paths:
        return all_mesh_objects

    wanted_paths = set(target_paths)
    wanted_names = set()
    for target in _target_files(session):
        if target["path"] not in wanted_paths:
            continue
        declared_names = _target_declared_mesh_names(target)
        if declared_names:
            wanted_names.update(declared_names)
        else:
            for entry in target["data"].get("smrPaths") or []:
                if not isinstance(entry, dict):
                    continue
                wanted_names.update(name for name in _target_entry_names(entry) if name)

    selected = []
    seen = set()
    for obj in all_mesh_objects:
        tagged_path = _mesh_tag_target_path(obj)
        if any(_target_path_matches(tagged_path, path) for path in wanted_paths):
            selected.append(obj)
            seen.add(obj.name)
        elif any(name in wanted_names for name in _mesh_candidate_names(obj)):
            selected.append(obj)
            seen.add(obj.name)

    return selected or all_mesh_objects


def _filter_meshes_for_request(session, mesh_objects, requested_mesh_name):
    if not requested_mesh_name:
        return mesh_objects, ""

    selected, display_names, _refresh_names = _filter_meshes_for_requests(session, mesh_objects, [requested_mesh_name])
    return selected, (display_names[0] if display_names else "")


def _filter_meshes_for_requests(session, mesh_objects, requested_mesh_names):
    requested_names = [name for name in (requested_mesh_names or []) if name]
    if not requested_names:
        return mesh_objects, [], []

    requested = []
    display_names = []
    requested_set = set(requested_names)
    for obj in mesh_objects:
        if requested_set.intersection(_mesh_candidate_names(obj)):
            requested.append(obj)
            display_names.append(obj.name)

    if not requested:
        return [], [], []

    target_paths = _target_paths_for_meshes(session, requested)
    group_meshes = _meshes_for_target_paths(session, mesh_objects, target_paths)
    return group_meshes, display_names, _manifest_mesh_names(requested)


def _manifest_mesh_names(mesh_objects):
    names = []
    seen = set()
    for obj in mesh_objects or []:
        for name in _mesh_candidate_names(obj):
            if name and name not in seen:
                names.append(name)
                seen.add(name)
    return names


def export_mesh_report(context, session):
    meshes, missing = find_export_meshes(context, session)
    return {
        "meshes": [obj.name for obj in meshes],
        "missing": missing,
        "targetNames": _target_mesh_names(session),
    }


def _export_fbx(context, mesh_objects, armature_obj, output_path):
    output_path = str(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    export_objects = list(mesh_objects)
    if armature_obj is not None:
        export_objects.append(armature_obj)

    with _objects_in_view_layer(context, export_objects), _preserved_selection(context):
        if context.object and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for obj in context.view_layer.objects:
            obj.select_set(False)
        for mesh_obj in mesh_objects:
            mesh_obj.select_set(True)
        if armature_obj is not None:
            armature_obj.select_set(True)
            context.view_layer.objects.active = armature_obj
        else:
            context.view_layer.objects.active = mesh_objects[0]

        kwargs = {
            "filepath": output_path,
            "use_selection": True,
            "object_types": {"ARMATURE", "MESH"},
            "apply_scale_options": "FBX_SCALE_ALL",
            "add_leaf_bones": False,
            "use_mesh_modifiers": False,
            "use_custom_props": True,
            "bake_anim": False,
            "path_mode": "AUTO",
        }

        bpy.ops.export_scene.fbx(**kwargs)


def _target_fbx_path(session, mesh_names=None):
    files = session.get("targetFbxFiles") or []
    if files:
        mesh_name_set = set(mesh_names or [])
        best_file = None
        best_score = -1
        for target in files:
            if not isinstance(target, dict):
                continue
            score = 0
            declared_names = _target_declared_mesh_names(target)
            for name in declared_names:
                if name in mesh_name_set:
                    score += 10
            for entry in target.get("smrPaths") or []:
                if isinstance(entry, dict) and entry.get("meshName") in mesh_name_set:
                    score += 1
            if score > best_score:
                best_file = target
                best_score = score
        first = best_file or files[0] or {}
        return first.get("unityPath") or first.get("path") or ""
    selected = session.get("selectedCustomBase") or {}
    files = selected.get("baseFbxFiles") or []
    return files[0] if files else ""


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def _send_to_unity(context, requested_mesh_names=None, automatic=False):
    global _IS_EXPORTING
    settings = get_settings(context)
    session = get_sync_session(settings)
    if settings is None or session is None:
        return False, "Connect Magic Sync with Unity before exporting"

    inbox_path = session.get("inboxPath")
    if not inbox_path:
        return False, "Magic Sync session has no Unity inbox"

    mesh_objects, missing_mesh_names = find_export_meshes(context, session)
    mesh_objects, requested_display_names, refresh_mesh_names = _filter_meshes_for_requests(session, mesh_objects, requested_mesh_names)
    if not mesh_objects:
        return False, "No Blender meshes matched the Unity target FBX renderer list"

    body_obj = settings.body_object if settings.body_object in mesh_objects else None
    if body_obj is None:
        body_obj = next((obj for obj in mesh_objects if obj.name == "Body"), mesh_objects[0])
    settings.body_object = body_obj

    armature_obj = settings.armature_object or _find_armature_for_body(context, body_obj)
    if armature_obj is not None:
        settings.armature_object = armature_obj

    xmuscle_result = {
        "available": False,
        "baked": False,
        "warnings": [],
        "muscles": [],
    }
    if settings.include_xmuscle and body_obj.name == "Body":
        xmuscle_result = xmuscle_bridge.bake_for_export(
            context,
            body_obj,
            armature_obj,
            force_rebake=settings.xmuscle_force_rebake,
        )

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    export_dir = Path(inbox_path) / ("export_" + timestamp)
    models_dir = export_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    mesh_names = _manifest_mesh_names(mesh_objects)
    mesh_groups = _mesh_groups_by_target(session, mesh_objects)
    model_entries = []
    _IS_EXPORTING = True
    try:
        for index, group in enumerate(mesh_groups, start=1):
            group_meshes = group["meshes"]
            target_fbx_path = group.get("targetFbxPath") or _target_fbx_path(session, [obj.name for obj in group_meshes])
            file_name = f"{index:02d}_{_safe_file_stem(target_fbx_path)}.fbx"
            fbx_path = models_dir / file_name
            _export_fbx(context, group_meshes, armature_obj, fbx_path)

            model_entries.append(
                {
                    "role": "CUSTOM_BASE",
                    "path": "models/" + file_name,
                    "targetFbxPath": target_fbx_path,
                    "primaryBodyObject": body_obj.name,
                    "armatureObject": armature_obj.name if armature_obj else "",
                    "meshNames": refresh_mesh_names or _manifest_mesh_names(group_meshes),
                    "missingUnityTargetMeshNames": missing_mesh_names,
                    "shapeKeysByMesh": _shape_keys_by_mesh(group_meshes),
                    "armatureBones": _armature_bone_names(armature_obj),
                    "fbxSettings": {
                        "applyScaleOptions": "FBX_SCALE_ALL",
                        "addLeafBones": False,
                        "useMeshModifiers": False,
                    },
                }
            )
    finally:
        _IS_EXPORTING = False

    custom_base = session.get("selectedCustomBase") or {}
    manifest = {
        "kind": EXPORT_KIND,
        "protocolVersion": PROTOCOL_VERSION,
        "sessionId": session.get("sessionId", ""),
        "token": session.get("token", ""),
        "createdAtUtc": datetime.datetime.utcnow().isoformat() + "Z",
        "source": {
            "blendFile": bpy.data.filepath,
            "addon": "MCB",
            "addonVersion": "0.1.0",
        },
        "target": {
            "unityProjectPath": session.get("unityProjectPath", ""),
            "customBaseName": custom_base.get("name", settings.custom_base_name or "Custom Base"),
            "targetFbxPath": _target_fbx_path(session, mesh_names),
        },
        "models": model_entries,
        "blendshapes": [],
        "xmuscle": xmuscle_result,
    }

    manifest_path = export_dir / "manifest.json"
    ready_path = export_dir / "ready.json"
    _write_json(manifest_path, manifest)
    _write_json(
        ready_path,
        {
            "kind": READY_KIND,
            "protocolVersion": PROTOCOL_VERSION,
            "sessionId": session.get("sessionId", ""),
            "token": session.get("token", ""),
            "manifestPath": "manifest.json",
            "createdAtUtc": datetime.datetime.utcnow().isoformat() + "Z",
        },
    )

    warnings = xmuscle_result.get("warnings") or []
    prefix = "Auto-synced " if automatic else "Synced "
    if requested_display_names:
        display = ", ".join(requested_display_names[:3])
        if len(requested_display_names) > 3:
            display += " +" + str(len(requested_display_names) - 3) + " more"
        settings.last_status = prefix + display + " via " + str(len(mesh_objects)) + " mesh(es) across " + str(len(model_entries)) + " FBX export(s) to Unity MCB inbox: " + str(export_dir)
    else:
        settings.last_status = prefix + str(len(mesh_objects)) + " mesh(es) across " + str(len(model_entries)) + " FBX export(s) to Unity MCB inbox: " + str(export_dir)
    if missing_mesh_names:
        settings.last_status += " (" + str(len(missing_mesh_names)) + " Unity target mesh name(s) not found in Blender)"
    if warnings:
        settings.last_status += " (" + str(len(warnings)) + " XMuscle warning(s))"
    _mark_meshes_clean(context.scene, settings, mesh_objects)
    return True, settings.last_status


class MCB_OT_send_to_unity(bpy.types.Operator):
    bl_idname = "mcb.send_to_unity"
    bl_label = "Sync with Unity"
    bl_description = "Export the custom base FBX and send it to the synced Unity MCB project"
    requested_mesh_name: StringProperty(default="")

    def execute(self, context):
        requested = [self.requested_mesh_name] if self.requested_mesh_name else []
        ok, message = _send_to_unity(context, requested_mesh_names=requested)
        self.report({"INFO"} if ok else {"ERROR"}, message)
        if not ok:
            return {"CANCELLED"}
        return {"FINISHED"}


def _dirty_mesh_names(settings):
    if settings is None or not settings.dirty_mesh_names_json:
        return []
    try:
        values = json.loads(settings.dirty_mesh_names_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def _set_dirty_mesh_names(settings, mesh_names):
    if settings is None:
        return
    clean_names = sorted(set(name for name in (mesh_names or []) if name))
    settings.dirty_mesh_names_json = json.dumps(clean_names) if clean_names else ""


def dirty_mesh_count(settings, scene=None):
    return len(dirty_mesh_name_set(settings, scene))


def dirty_mesh_name_set(settings, scene=None):
    names = set(_dirty_mesh_names(settings))
    if scene is not None:
        target_names = _target_scene_mesh_names(scene, settings)
        if target_names:
            names.intersection_update(target_names)
    return names


def _load_dirty_signatures(settings):
    if settings is None or not settings.dirty_mesh_signatures_json:
        return {}
    try:
        values = json.loads(settings.dirty_mesh_signatures_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(values, dict):
        return {}
    return {
        key: value
        for key, value in values.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _set_dirty_signatures(settings, signatures):
    if settings is None:
        return
    clean = {
        key: value
        for key, value in (signatures or {}).items()
        if isinstance(key, str) and isinstance(value, str)
    }
    settings.dirty_mesh_signatures_json = json.dumps(clean, sort_keys=True) if clean else ""


def _mesh_signature(mesh_obj):
    data = getattr(mesh_obj, "data", None)
    digest = hashlib.sha256()
    digest.update((getattr(mesh_obj, "name", "") + "\n").encode("utf-8"))
    digest.update((getattr(data, "name", "") + "\n").encode("utf-8"))
    if data is None:
        return digest.hexdigest()

    digest.update(f"{len(data.vertices)}|{len(data.edges)}|{len(data.polygons)}\n".encode("utf-8"))
    for vertex in data.vertices:
        digest.update(f"{vertex.co.x:.6f},{vertex.co.y:.6f},{vertex.co.z:.6f};".encode("utf-8"))

    shape_keys = getattr(data, "shape_keys", None)
    if shape_keys is not None:
        for key in shape_keys.key_blocks:
            digest.update(f"\nkey:{key.name}|{key.value:.6f}|{len(key.data)}\n".encode("utf-8"))
            for point in key.data:
                digest.update(f"{point.co.x:.6f},{point.co.y:.6f},{point.co.z:.6f};".encode("utf-8"))
    return digest.hexdigest()


def _target_scene_meshes(scene, settings):
    session = get_sync_session(settings)
    target_names = set(_target_mesh_names(session)) if session else set()
    if not target_names:
        return []

    meshes = []
    seen = set()
    for obj in scene.objects:
        if obj.type != "MESH" or getattr(obj, "Muscle_XID", False):
            continue
        if target_names.intersection(_mesh_candidate_names(obj)):
            if obj.name not in seen:
                meshes.append(obj)
                seen.add(obj.name)
    return meshes


def _target_scene_mesh_names(scene, settings):
    return {obj.name for obj in _target_scene_meshes(scene, settings)}


def _target_mesh_signature_map(scene, settings):
    return {obj.name: _mesh_signature(obj) for obj in _target_scene_meshes(scene, settings)}


def _mesh_signature_map_for_names(scene, settings, mesh_names):
    wanted = set(mesh_names or [])
    return {
        obj.name: _mesh_signature(obj)
        for obj in _target_scene_meshes(scene, settings)
        if obj.name in wanted
    }


def initialize_dirty_tracking_baseline_for_scene(scene):
    if scene is None:
        return
    settings = getattr(scene, "mcb_blender", None)
    if settings is None:
        return
    _set_dirty_signatures(settings, _target_mesh_signature_map(scene, settings))
    _set_dirty_mesh_names(settings, [])


def _mark_meshes_clean(scene, settings, mesh_objects):
    if scene is None or settings is None:
        return
    dirty = set(_dirty_mesh_names(settings))
    for obj in mesh_objects or []:
        if obj is None:
            continue
        dirty.discard(obj.name)
    _set_dirty_mesh_names(settings, dirty)


def _mark_dirty_meshes(scene, mesh_names):
    settings = getattr(scene, "mcb_blender", None)
    if settings is None or not settings.sync_on_save:
        return
    if time.time() < getattr(settings, "sync_on_save_ignore_until", 0.0):
        return
    target_mesh_names = _target_scene_mesh_names(scene, settings)
    if target_mesh_names:
        mesh_names = [name for name in mesh_names if name in target_mesh_names]
        if not mesh_names:
            return

    dirty = set(dirty_mesh_name_set(settings, scene))
    for name in mesh_names:
        dirty.add(name)
    _set_dirty_mesh_names(settings, dirty)


def _objects_for_mesh_data(scene, mesh_data):
    if scene is None or mesh_data is None:
        return []
    return [
        obj
        for obj in scene.objects
        if obj.type == "MESH"
        and not getattr(obj, "Muscle_XID", False)
        and getattr(obj, "data", None) == mesh_data
    ]


def _objects_for_shape_keys(scene, shape_keys):
    if scene is None or shape_keys is None:
        return []
    return [
        obj
        for obj in scene.objects
        if obj.type == "MESH"
        and not getattr(obj, "Muscle_XID", False)
        and getattr(getattr(obj, "data", None), "shape_keys", None) == shape_keys
    ]


@persistent
def _depsgraph_update_handler(scene, depsgraph):
    if _IS_EXPORTING or scene is None:
        return
    settings = getattr(scene, "mcb_blender", None)
    if settings is None or not settings.sync_on_save:
        return
    if time.time() < getattr(settings, "sync_on_save_ignore_until", 0.0):
        return

    dirty_names = set()
    for update in depsgraph.updates:
        data_block = update.id
        if isinstance(data_block, bpy.types.Object):
            if not getattr(update, "is_updated_geometry", True):
                continue
            if data_block.type == "MESH" and not getattr(data_block, "Muscle_XID", False):
                dirty_names.add(data_block.name)
        elif isinstance(data_block, bpy.types.Mesh):
            dirty_names.update(obj.name for obj in _objects_for_mesh_data(scene, data_block))
        elif isinstance(data_block, bpy.types.Key):
            dirty_names.update(obj.name for obj in _objects_for_shape_keys(scene, data_block))

    if dirty_names:
        _mark_dirty_meshes(scene, dirty_names)


@persistent
def _save_post_handler(_dummy):
    global _SAVE_SYNC_TIMER_PENDING
    if _SAVE_SYNC_TIMER_PENDING:
        return

    should_schedule = False
    for scene in bpy.data.scenes:
        settings = getattr(scene, "mcb_blender", None)
        if settings is not None and settings.sync_on_save and get_sync_session(settings):
            should_schedule = True
            break

    if not should_schedule:
        return

    _SAVE_SYNC_TIMER_PENDING = True
    bpy.app.timers.register(_run_save_sync_timer, first_interval=SAVE_SYNC_DELAY_SECONDS)


def _run_save_sync_timer():
    global _SAVE_SYNC_TIMER_PENDING
    _SAVE_SYNC_TIMER_PENDING = False

    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None or not getattr(window_manager, "windows", None):
        return None

    context = bpy.context
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "mcb_blender", None) if scene else None
    if settings is None or not settings.sync_on_save or not get_sync_session(settings):
        return None

    dirty_names = sorted(dirty_mesh_name_set(settings, scene))
    target_mesh_names = _target_scene_mesh_names(scene, settings)
    if target_mesh_names:
        dirty_names = [name for name in dirty_names if name in target_mesh_names]
        _set_dirty_mesh_names(settings, dirty_names)
    if not dirty_names:
        settings.last_status = "Sync on Save skipped: no modified MCB meshes detected"
        return None

    requested = dirty_names
    ok, message = _send_to_unity(context, requested_mesh_names=requested, automatic=True)
    if ok:
        _set_dirty_mesh_names(settings, [])
    settings.last_status = message
    print("[MCB] " + message)
    return None


def register_save_handlers():
    if _depsgraph_update_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_update_handler)
    if _save_post_handler not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(_save_post_handler)


def unregister_save_handlers():
    if _depsgraph_update_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_update_handler)
    if _save_post_handler in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_save_post_handler)
