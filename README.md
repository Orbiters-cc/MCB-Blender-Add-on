# MCB Blender Addon

MCB is a Blender addon that sends a custom base FBX to the Unity MCB package through Magic Sync.

## Workflow

1. In Unity, open an avatar with `My Custom Base`, enable Creator Mode, and click `Sync with Blender`.
2. In Blender, open `View3D > Sidebar > MCB`.
3. Click `Connect Magic Sync From Clipboard`.
4. Check the auto-detected mesh list and pick an armature if needed.
5. Click `Send to Unity (<custom base name>)`.

You can also start from Blender: click `Start Sync`, then click `Sync with Blender` in Unity. The addon exports FBX files with shape keys preserved, writes a manifest package into Unity's sync inbox, and drops a `ready.json` marker. Unity then imports the package, creates `.fbx.old` when needed, overwrites the matched target FBX, and refreshes the SkinnedMeshRenderer paths Unity provided during sync.

## XMuscle Orbit Helper

If XMuscle Orbit Helper is installed and `Include XMuscle deformation` is enabled, MCB calls its public API to bake muscle deformation before export and includes the generated metadata in the manifest.

## Development

Build:

```powershell
.\package_addon.bat
```

Sync into Blender 5.0 addon folder:

```powershell
.\dev_sync_addon.bat
```
