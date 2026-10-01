# AGENTS.md

## Purpose

This repository contains the Blender-side MCB bridge addon. It is a generic custom-base export tool and must not depend on muscle-specific code unless the optional XMuscle Orbit Helper adapter is available.

## Architecture Rules

- Keep Unity sync/session parsing in `mcb_blender/sync.py`.
- Keep FBX/package export logic in `mcb_blender/exporter.py`.
- Keep optional XMuscle Orbit Helper integration in `mcb_blender/xmuscle_bridge.py`.
- Keep UI drawing in `mcb_blender/ui.py`.
- Do not import XMuscle Orbit Helper at module import time. Import it lazily only when exporting with muscle automation enabled.

## Export Rules

- Preserve shape keys in FBX exports.
- Use FBX Apply Scalings = FBX All.
- Disable Add Leaf Bones.
- Keep the export package plain and inspectable: `manifest.json`, `models/*.fbx`, and a final `ready.json` marker.

## Packaging Rules

- The add-on is a Blender extension: `mcb_blender/blender_manifest.toml` holds the only copy of the version (`sync.ADDON_VERSION` reads it). Never add `bl_info` or another version literal.
- Unity MCB downloads the release asset `mcb_blender-<version>.zip` for the version in its `BlenderAddonService`; keep both in step when releasing.

## Checks

Run before packaging (Python 3.11, the oldest Python of the supported Blender versions):

```powershell
python -m py_compile .\mcb_blender\__init__.py .\mcb_blender\properties.py .\mcb_blender\sync.py .\mcb_blender\launch.py .\mcb_blender\xmuscle_bridge.py .\mcb_blender\exporter.py .\mcb_blender\ui.py
```

Then build and sync (the build validates the manifest):

```powershell
.\package_addon.bat "<path to blender.exe>"
.\dev_sync_addon.bat
```
