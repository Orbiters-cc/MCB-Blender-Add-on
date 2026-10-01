# MCB Blender Extension

MCB is a Blender extension (Blender 5.0+) that sends a custom base FBX to the Unity MCB package through Magic Sync.

## Install

Unity MCB installs it for you: `Modify with Blender` downloads the release zip that matches the MCB package and runs

```powershell
blender --command extension install-file --repo user_default --enable mcb_blender-<version>.zip
```

To install it by hand, use the same command or `Edit > Preferences > Get Extensions > Install from Disk`.

## Workflow

1. In Unity, open an avatar with `My Custom Base`, enable Creator Mode, and click `Sync with Blender`.
2. In Blender, open `View3D > Sidebar > MCB`.
3. Click `Connect Magic Sync From Clipboard`.
4. Check the auto-detected mesh list and pick an armature if needed.
5. Click `Send to Unity (<custom base name>)`.

You can also start from Blender: click `Start Sync`, then click `Sync with Blender` in Unity. The extension exports FBX files with shape keys preserved, writes a manifest package into Unity's sync inbox, and drops a `ready.json` marker. Unity then imports the package, creates `.fbx.old` when needed, overwrites the matched target FBX, and refreshes the SkinnedMeshRenderer paths Unity provided during sync.

## XMuscle Orbit Helper

If XMuscle Orbit Helper is installed and `Include XMuscle deformation` is enabled, MCB calls its public API to bake muscle deformation before export and includes the generated metadata in the manifest.

## Development

Build `dist\mcb_blender-<version>.zip` (needs a Blender 5.0+ executable; the build validates `blender_manifest.toml`):

```powershell
.\package_addon.bat "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"
```

Sync the sources into the local `user_default` extension repository of Blender 5.0 (or pass another version, e.g. `5.2`), then enable `MCB` once in Preferences:

```powershell
.\dev_sync_addon.bat
```

## Release

The version is only written in `mcb_blender/blender_manifest.toml`. To release, bump it, set the same version in Unity MCB (`BlenderAddonService.BlenderAddonVersion`), and push the tag `v<version>`: the workflow publishes `mcb_blender-<version>.zip`, the asset Unity downloads.
