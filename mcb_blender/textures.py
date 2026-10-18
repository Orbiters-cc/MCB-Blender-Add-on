"""
Images Unity forwards from Substance 3D Painter (live while painting, then the maps applied when Painter saves), set on
the material of the mesh's slot they paint: one Image Texture node per channel, wired into the Principled BSDF.
"""

import array
import os

import bpy

# Painter channel -> Principled BSDF input; the normal goes through a Normal Map node (Painter's normals are OpenGL, as
# Blender's), ambient occlusion has no input.
_INPUTS = {
    "basecolor": "Base Color",
    "roughness": "Roughness",
    "metallic": "Metallic",
    "emissive": "Emission Color",
    "normal": "Normal",
}
_ORDER = ("basecolor", "metallic", "roughness", "normal", "emissive")
_COLOR = {"basecolor", "emissive"}


def apply(obj, slot, channel, path):
    """
    Shows the image at path for channel on the material of obj that Unity's material slot (slot) shows; False when there
    is nothing to set it on.
    """
    index = blender_slot(obj, slot)
    if channel not in _INPUTS or not os.path.isfile(path) or index is None:
        return False
    material = obj.material_slots[index].material
    if material is None:
        return False
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next((node for node in tree.nodes if node.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None or _INPUTS[channel] not in bsdf.inputs:
        return False

    image = _image(material.name + " (Painter " + channel + ")", path, channel in _COLOR)
    name = "MCB Painter " + channel
    node = tree.nodes.get(name)
    if node is None:
        node = tree.nodes.new("ShaderNodeTexImage")
        node.name = node.label = name
        node.location = (bsdf.location.x - 700, bsdf.location.y - 280 * _ORDER.index(channel))
    node.image = image

    target = bsdf.inputs[_INPUTS[channel]]
    if channel == "normal":
        normal_map = tree.nodes.get("MCB Painter normal map")
        if normal_map is None:
            normal_map = tree.nodes.new("ShaderNodeNormalMap")
            normal_map.name = normal_map.label = "MCB Painter normal map"
            normal_map.location = (bsdf.location.x - 300, node.location.y)
        _link(tree, node.outputs["Color"], normal_map.inputs["Color"])
        _link(tree, normal_map.outputs["Normal"], target)
    else:
        _link(tree, node.outputs["Color"], target)
        if channel == "emissive" and "Emission Strength" in bsdf.inputs and bsdf.inputs["Emission Strength"].default_value == 0:
            bsdf.inputs["Emission Strength"].default_value = 1.0
    return True


def blender_slot(obj, unity_slot):
    """
    The material slot of obj that is Unity's slot unity_slot: Unity orders a mesh's slots by its polygons' first use of
    each material (the FBX export keeps Blender's polygon order); None when there is no such slot.
    """
    data = obj.data
    indexes = array.array("i", bytes(4 * len(data.polygons)))
    data.polygons.foreach_get("material_index", indexes)
    order = []
    for index in indexes:
        if index not in order:
            order.append(index)
            if len(order) > unity_slot:
                break
    if not order:
        order = [0]
    if not 0 <= unity_slot < len(order) or not 0 <= order[unity_slot] < len(obj.material_slots):
        return None
    return order[unity_slot]


def _image(name, path, color):
    """The material's image of that channel, now the file at path (loaded again: Painter rewrites its files)."""
    image = bpy.data.images.get(name)
    if image is None:
        image = bpy.data.images.load(path, check_existing=False)
        image.name = name
    elif os.path.normcase(bpy.path.abspath(image.filepath)) != os.path.normcase(path):
        image.filepath = path
        image.reload()
    else:
        image.reload()
    try:
        image.colorspace_settings.name = "sRGB" if color else "Non-Color"
    except TypeError:  # A colour management config without those names keeps its default.
        pass
    return image


def _link(tree, output, target):
    if not any(link.from_socket == output for link in target.links):
        tree.links.new(output, target)
