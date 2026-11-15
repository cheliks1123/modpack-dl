# modpack-dl

Utility for Legacy Launcher: drop in a modpack from Modrinth or CurseForge and it
downloads all the mods for you. Standard-library Python 3.8+, nothing to install.

```
python modpack_dl.py MyPack.mrpack                 # Modrinth
python modpack_dl.py MyPack.zip --curseforge-key KEY   # CurseForge (or env CF_API_KEY)
python modpack_dl.py MyPack.mrpack --out "%APPDATA%\.minecraft"
```

What it does
- `.mrpack` (Modrinth): downloads every file from `modrinth.index.json`, verifies sha1/sha512,
  skips files marked unsupported for your side, extracts `overrides/` (+ `client-overrides/`).
- `.zip` (CurseForge): reads `manifest.json`, resolves files through the CurseForge API,
  puts mods in `mods/`, resource packs in `resourcepacks/`, shaders in `shaderpacks/`,
  extracts `overrides/`.
- Prints the Minecraft version and loader (Forge / NeoForge / Fabric / Quilt) the pack needs.
  Install that version in Legacy Launcher once, then launch with the output folder as game directory.
- Re-running is safe: files that are already present and valid are skipped.

Options: `--out DIR`, `--side client|server`, `--jobs N`, `--dry-run`, `--allow-host HOST`.

Notes
- CurseForge needs a free API key: https://console.curseforge.com/
- Some CurseForge mods forbid third-party downloads; those are listed with a link at the end
  so you can add them manually.
- Downloads are limited to Modrinth/GitHub/GitLab/CurseForge CDN hosts, and every path in a
  pack is checked so a pack cannot write outside the output folder.

## Windows installer

Every push builds `modpack-dl.exe` (PyInstaller) and an Inno Setup installer
(`installer/modpack-dl.iss`) in GitHub Actions (`.github/workflows/build.yml`).
- Download the result from the run's **Artifacts**, or
- push a tag like `v1.0.0` and the installer is attached to a GitHub Release automatically.

The installer is per-user (no admin rights), can add the tool to `PATH` and adds a
right-click item "Download mods (modpack-dl)" for `.mrpack` files.

Local build (Windows): `pip install pyinstaller`, `pyinstaller --onefile --name modpack-dl modpack_dl.py`,
then `ISCC.exe /DMyAppVersion=1.0.0 installer\modpack-dl.iss`.

Tests: `python -m unittest discover -s tests -v`
