# BeamNG Mod Pack Manager

[Repository](https://github.com/Alyndiar/BeamNG-Manager)
[Latest Release](https://github.com/Alyndiar/BeamNG-Manager/releases/latest)

Windows desktop mod manager for BeamNG.drive, focused on local mod organization, pack workflows, and safe active-state sync.

## Current Product State

- The app is now **Local-only** in the main UI (Online/Console tabs were removed).
- Browser integration is handled by separate extensions in `integrations/` (Firefox + Chromium build).
- `info.json` processing is centralized and cached for both list metadata and viewer usage.

## Main Features

### Local Mod Management

- Scans and organizes mods from:
  - `mods` root
  - `mods/repo`
  - pack folders in `Library Root`
  - orphan/unknown junction targets
- Pack enable/disable using NTFS junction behavior.
- Move mods between packs and `mods` root.
- Multi-select actions (enable/disable/move/delete where allowed).
- Duplicate finder with delete callback support.
- `mods/repo` protections are enforced for destructive actions.

### Views and Metadata

- Text view and icon view.
- Sort modes in toolbar (`Name`, `Tags`, `Category`, `Size`).
- Category badges (repo category and type-derived fallback for non-repo mods where available).
- Prefix/tag badges (Alpha/Beta/Experimental/etc.).
- Optional info-label display.
- `View Metadata` action opens a non-modal `info.json` viewer:
  - cleaned top `message` section (if present)
  - tree view with expand/collapse controls
  - copy JSON / copy message
  - valid/recovered/invalid/missing handling

### Profiles and Active State

- Profile save/load with pack-state + mod active-state.
- Conflict resolution against `db.json` active states.
- Debounced/safe `db.json` synchronization.

### Runtime Safety

- File-mutating actions are blocked while `BeamNG.drive.exe` is running.
- Runtime status is polled in background.
- Pending writes/worker tasks are flushed on shutdown.

## Browser Bridge + Extensions

Extensions live in:

- `integrations/firefox-beamng-manager`
- `integrations/chrome-beamng-manager`
- Firefox Add-ons listing: <https://addons.mozilla.org/fr/firefox/addon/beamng-manager-bridge/>
- Chrome Web Store listing: <https://chromewebstore.google.com/detail/hhilmajldhikjnjeafjfihpnkmbodggh>

### What the extension does

- Shows installed/subscribed badges/highlights on BeamNG resources/forums pages.
- Receives bridge open-url commands from manager (`Open in browser` action for repo mods).
- Popup/options controls:
  - bridge port
  - poll interval
  - reconnect bridge
  - last command status (opened/failed + timestamp)

### Bridge protocol (manager localhost server)

- `GET /session/start`
- `GET /changes`
- `GET /markers`
- `GET /commands/next`
- legacy compatibility: `GET /installed-markers`

### Bridge debug logging

- Manager setting: `Settings -> Bridge Debug`.
- When enabled, protocol actions are printed to console with `[BridgeDebug ...]`.

## Requirements

- Windows (NTFS required)
- Python 3.10+ (tested with 3.12)
- PySide6
- Optional for development: `pytest`

## Ways to Use

1. Standalone release build (no Python setup required):
- Download from GitHub Releases:
  - `BeamNG-Manager-<version>-windows-x64.zip` (recommended)
  - `BeamNG-Manager-<version>-windows-x64.exe` (single-file executable)
- Extract the ZIP to its own folder and run the EXE from there.
- Keep the EXE in that folder before first launch; runtime folders are created beside it (`.cache/`, `Profiles/`).

2. Manual install from GitHub source:
- Clone this repository.
- Create/use the `BeamNG-Manager` conda environment.
- Install dependencies and run with Python.

## Manual Install (Source)

```powershell
git clone <repo-url>
cd BeamNG-Manager
conda create -n BeamNG-Manager python=3.12 -y
conda activate BeamNG-Manager
python -m pip install PySide6 pytest
```

## Run

```powershell
conda run -n BeamNG-Manager python -m app.main
```

## Executable Builds (PyInstaller)

Important first-run rule for EXE builds:

- Put the EXE in its own folder before running it for the first time.
- The app creates runtime data folders relative to the EXE folder:
  - `.cache/`
  - `Profiles/`

`QSettings` stays in the normal Windows registry location.

Local build commands:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_release_artifact.ps1 -Variant gui -Version 0.5.1
powershell -ExecutionPolicy Bypass -File scripts\build_release_artifact.ps1 -Variant gui -Version 0.5.1 -IncludeRawExe
powershell -ExecutionPolicy Bypass -File scripts\build_release_artifact.ps1 -Variant debug -Version 0.5.1
```

The debug-console variant is intended for local troubleshooting only and is not published as a normal release artifact.

GitHub releases:

- Tag push triggers `.github/workflows/release.yml`.
- The workflow builds the GUI EXE and uploads:
  - `BeamNG-Manager-<version>-windows-x64.zip` + SHA256
  - `BeamNG-Manager-<version>-windows-x64.exe` + SHA256
- Browser extensions are distributed through their store listings (not packaged as release assets).

## First Launch

Set paths in `Settings...`:

- `BeamNG Mod Folder`
- `Library Root Folder`
- `Open Repo URL via` (`Default browser` or `Bridge`)
- `Browser Bridge Port`
- `Bridge Debug` (optional)

Settings are stored in `QSettings` under:

- Org: `BeamNGManager`
- App: `ModPackManager`

## Extension Packaging

Firefox (unsigned XPI):

```powershell
powershell -ExecutionPolicy Bypass -File integrations\firefox-beamng-manager\package_xpi.ps1
```

Chromium (zip for unpacked/load):

```powershell
powershell -ExecutionPolicy Bypass -File integrations\chrome-beamng-manager\package_zip.ps1
```

## Development Commands

Run tests:

```powershell
conda run -n BeamNG-Manager python -m pytest -q
```

Quick compile check:

```powershell
conda run -n BeamNG-Manager python -m py_compile ui/main_window.py
```

## Project Layout

```text
app/
core/
ui/
tests/
integrations/
packaging/
scripts/
```
