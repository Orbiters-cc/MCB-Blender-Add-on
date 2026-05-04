import json
import os
import re
from pathlib import Path

import bpy

from .sync import apply_sync_session, write_heartbeat

LAUNCH_KIND = "orbiters.mcb.blenderLaunch"
PROTOCOL_VERSION = 1
TARGET_FBX_PROP = "mcb_target_fbx_path"
TARGET_MESH_PROP = "mcb_target_mesh_name"
TARGET_RENDERER_PROP = "mcb_target_renderer_name"


def _safe_name(value, fallback="MCB"):
    name = Path(value or fallback).stem or fallback
    return re.sub(r"[^A-Za-z0-9_. -]+", "_", name).strip() or fallback


def _strip_blender_suffix(name):
    if not name:
        return ""
    return re.sub(r"\.\d{3}$", "", name)


def _normalize_path(path):
    return (path or "").replace("\\", "/").lower()


def _target_path(target):
    return target.get("unityPath") or target.get("path") or ""


def _target_absolute_path(target):
    return target.get("absolutePath") or target.get("absolute") or ""


def _entry_names(entry):
    return [
        entry.get("meshName"),
        entry.get("rendererName"),
        Path(entry.get("fbxMeshPath") or "").name,
    ]


def _target_known_names(target):
    names = []
    for value in target.get("meshNames") or target.get("fbxMeshNames") or []:
        if isinstance(value, str) and value:
            names.append(value)
    for entry in target.get("smrPaths") or []:
        if not isinstance(entry, dict):
            continue
        names.extend(value for value in _entry_names(entry) if value)
    return names


def _object_candidate_names(obj):
    values = [getattr(obj, "name", "")]
    data = getattr(obj, "data", None)
    values.append(getattr(data, "name", ""))
    names = []
    for value in values:
        if not value:
            continue
        names.append(value)
        stripped = _strip_blender_suffix(value)
        if stripped and stripped != value:
            names.append(stripped)
    return names


def _best_mesh_name(obj, target):
    known = set(_target_known_names(target))
    for name in _object_candidate_names(obj):
        if name in known:
            return name
    data = getattr(obj, "data", None)
    return getattr(data, "name", "") or getattr(obj, "name", "")


def _best_renderer_name(obj, target):
    for entry in target.get("smrPaths") or []:
        if not isinstance(entry, dict):
            continue
        names = set(value for value in _entry_names(entry) if value)
        if names.intersection(_object_candidate_names(obj)):
            return entry.get("rendererName") or ""
    return getattr(obj, "name", "")


def _ensure_collection(scene, name):
    base_name = _safe_name(name)
    collection_name = base_name
    index = 2
    while collection_name in bpy.data.collections:
        collection_name = f"{base_name} {index}"
        index += 1
    collection = bpy.data.collections.new(collection_name)
    scene.collection.children.link(collection)
    return collection


def _tag_imported_objects(objects, target):
    unity_path = _target_path(target)
    absolute_path = _target_absolute_path(target)
    for obj in objects:
        obj[TARGET_FBX_PROP] = unity_path
        obj["mcb_source_fbx_absolute_path"] = absolute_path
        if obj.type == "MESH":
            obj[TARGET_MESH_PROP] = _best_mesh_name(obj, target)
            obj[TARGET_RENDERER_PROP] = _best_renderer_name(obj, target)


def _material_candidate_names(obj):
    values = [
        getattr(obj, "name", ""),
        getattr(getattr(obj, "data", None), "name", ""),
        obj.get(TARGET_MESH_PROP, "") if obj is not None else "",
        obj.get(TARGET_RENDERER_PROP, "") if obj is not None else "",
    ]
    names = []
    for value in values:
        if not value:
            continue
        names.append(value)
        stripped = _strip_blender_suffix(value)
        if stripped and stripped != value:
            names.append(stripped)
    return set(names)


def _target_materials_for_object(obj, target):
    candidates = _material_candidate_names(obj)
    matches = []
    for material in target.get("materials") or []:
        if not isinstance(material, dict):
            continue
        names = set(
            value
            for value in (material.get("rendererName"), material.get("meshName"))
            if isinstance(value, str) and value
        )
        if names and not candidates.intersection(names):
            continue
        matches.append(material)
    return sorted(matches, key=lambda item: int(item.get("slot", 0) or 0))


def _principled_node(material):
    if not material.use_nodes:
        material.use_nodes = True
    for node in material.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return material.node_tree.nodes.new("ShaderNodeBsdfPrincipled")


def _node_input(node, names):
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return socket
    return None


def _set_node_input(node, names, value):
    socket = _node_input(node, names)
    if socket is not None:
        socket.default_value = value


def _texture_path(texture_info):
    if not isinstance(texture_info, dict):
        return ""
    path = texture_info.get("absolutePath") or texture_info.get("path") or ""
    return path if path and os.path.exists(path) else ""


def _add_image_texture(material, texture_info, colorspace="sRGB"):
    path = _texture_path(texture_info)
    if not path:
        return None
    try:
        image = bpy.data.images.load(path, check_existing=True)
    except RuntimeError:
        return None
    try:
        image.colorspace_settings.name = colorspace
    except Exception:
        pass
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    return node


def _connect_texture(material, texture_info, bsdf, input_names, colorspace="sRGB"):
    texture_node = _add_image_texture(material, texture_info, colorspace)
    socket = _node_input(bsdf, input_names)
    output = texture_node.outputs.get("Color") if texture_node is not None else None
    if output is not None and socket is not None:
        material.node_tree.links.new(output, socket)


def _create_material_from_unity(material_info):
    name = _safe_name(material_info.get("materialName") or "Unity Material", "Unity Material")
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    bsdf = _principled_node(material)

    color = material_info.get("baseColor") if isinstance(material_info.get("baseColor"), dict) else {}
    base_color = (
        float(color.get("r", 1.0)),
        float(color.get("g", 1.0)),
        float(color.get("b", 1.0)),
        float(color.get("a", 1.0)),
    )
    material.diffuse_color = base_color
    _set_node_input(bsdf, ("Base Color",), base_color)
    _set_node_input(bsdf, ("Metallic",), float(material_info.get("metallic", 0.0) or 0.0))
    _set_node_input(bsdf, ("Roughness",), float(material_info.get("roughness", 0.5) or 0.5))
    _set_node_input(bsdf, ("Alpha",), base_color[3])

    _connect_texture(material, material_info.get("baseColorTexture"), bsdf, ("Base Color",), "sRGB")
    _connect_texture(material, material_info.get("metallicTexture"), bsdf, ("Metallic",), "Non-Color")
    if _texture_path(material_info.get("roughnessTexture")):
        _connect_texture(material, material_info.get("roughnessTexture"), bsdf, ("Roughness",), "Non-Color")
    elif _texture_path(material_info.get("smoothnessTexture")):
        smoothness_texture = _add_image_texture(material, material_info.get("smoothnessTexture"), "Non-Color")
        roughness_socket = _node_input(bsdf, ("Roughness",))
        if smoothness_texture is not None and roughness_socket is not None:
            invert_node = material.node_tree.nodes.new("ShaderNodeInvert")
            color_output = smoothness_texture.outputs.get("Color")
            color_input = invert_node.inputs.get("Color")
            invert_output = invert_node.outputs.get("Color")
            if color_output is not None and color_input is not None and invert_output is not None:
                material.node_tree.links.new(color_output, color_input)
                material.node_tree.links.new(invert_output, roughness_socket)

    normal_texture = _add_image_texture(material, material_info.get("normalTexture"), "Non-Color")
    normal_socket = _node_input(bsdf, ("Normal",))
    if normal_texture is not None and normal_socket is not None:
        normal_map = material.node_tree.nodes.new("ShaderNodeNormalMap")
        color_output = normal_texture.outputs.get("Color")
        color_input = normal_map.inputs.get("Color")
        normal_output = normal_map.outputs.get("Normal")
        if color_output is not None and color_input is not None and normal_output is not None:
            material.node_tree.links.new(color_output, color_input)
            material.node_tree.links.new(normal_output, normal_socket)

    return material


def _apply_target_materials(objects, target):
    if not target.get("materials"):
        return
    for obj in objects:
        if obj.type != "MESH":
            continue
        material_infos = _target_materials_for_object(obj, target)
        if not material_infos:
            continue
        for material_info in material_infos:
            slot = int(material_info.get("slot", 0) or 0)
            material = _create_material_from_unity(material_info)
            while len(obj.data.materials) <= slot:
                obj.data.materials.append(None)
            obj.data.materials[slot] = material


def _has_tagged_mesh(scene, target):
    path = _normalize_path(_target_path(target))
    if not path:
        return False
    for obj in scene.objects:
        if obj.type == "MESH" and _normalize_path(obj.get(TARGET_FBX_PROP, "")) == path:
            return True
    return False


def _clear_scene(scene):
    for obj in scene.objects:
        obj.select_set(True)
    if scene.objects:
        bpy.ops.object.delete()


def _import_target_fbx(scene, target):
    fbx_path = _target_absolute_path(target)
    if not fbx_path or not os.path.exists(fbx_path):
        return []

    collection = _ensure_collection(scene, _target_path(target) or fbx_path)
    before = set(obj.name for obj in bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=fbx_path)
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    for obj in imported:
        if collection.objects.get(obj.name) is None:
            collection.objects.link(obj)
    _tag_imported_objects(imported, target)
    _apply_target_materials(imported, target)
    return imported


def _ensure_targets_imported(scene, session, force_import_all=False):
    imported_count = 0
    for target in session.get("targetFbxFiles") or []:
        if not isinstance(target, dict):
            continue
        if not force_import_all and _has_tagged_mesh(scene, target):
            continue
        imported_count += len(_import_target_fbx(scene, target))
    return imported_count


def _write_session_text(session):
    text = bpy.data.texts.get("MCB Magic Sync Session") or bpy.data.texts.new("MCB Magic Sync Session")
    text.clear()
    text.write(json.dumps(session, indent=2, sort_keys=True))


def _apply_session(session):
    scene = bpy.context.scene
    settings = getattr(scene, "mcb_blender", None)
    if settings is None:
        raise RuntimeError("MCB addon settings are unavailable after enabling the addon.")
    apply_sync_session(settings, session)
    settings.sync_on_save = True
    write_heartbeat(settings)
    return settings


def run_launch_config(config_path):
    with open(config_path, "r", encoding="utf-8") as handle:
        config = json.load(handle)

    if config.get("kind") != LAUNCH_KIND:
        raise RuntimeError("Launch config is not an MCB Blender launch payload.")
    if int(config.get("protocolVersion", 0)) > PROTOCOL_VERSION:
        raise RuntimeError("Unity uses a newer MCB Blender launch protocol.")

    session = config.get("syncSession")
    if not isinstance(session, dict):
        raise RuntimeError("Launch config is missing the Magic Sync session.")

    project = config.get("project") if isinstance(config.get("project"), dict) else {}
    project_path = project.get("projectAbsolutePath") or project.get("absolutePath") or ""
    if not project_path:
        raise RuntimeError("Launch config is missing the Blender project path.")

    project_path = os.path.abspath(project_path)
    os.makedirs(os.path.dirname(project_path), exist_ok=True)

    existing_project = os.path.exists(project_path)
    if existing_project:
        bpy.ops.wm.open_mainfile(filepath=project_path)
    else:
        _clear_scene(bpy.context.scene)

    settings = _apply_session(session)
    imported_count = _ensure_targets_imported(bpy.context.scene, session, force_import_all=not existing_project)
    _write_session_text(session)
    settings.last_status = "Opened from Unity MCB. Sync on Save is enabled."
    if imported_count:
        settings.last_status += f" Imported {imported_count} object(s)."

    bpy.ops.wm.save_as_mainfile(filepath=project_path)
    write_heartbeat(settings)
    return {"projectPath": project_path, "importedObjects": imported_count}
