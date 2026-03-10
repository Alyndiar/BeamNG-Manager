# BeamNG Mod Pack Manager

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

## Recommended Environment

```powershell
conda create -n BeamNG-Manager python=3.12 -y
conda activate BeamNG-Manager
python -m pip install PySide6 pytest
```

## Run

```powershell
conda run -n BeamNG-Manager python -m app.main
```

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
```
