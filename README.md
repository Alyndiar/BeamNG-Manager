# BeamNG Mod Pack Manager

Windows-only BeamNG mod pack manager with a PySide6 (Qt6) GUI.

It lets you:
- Manage pack activation through NTFS directory junctions.
- Browse mods from multiple sources (mods root, repo, packs, unknown junctions, orphan folders).
- Read `info.json` metadata lazily from mod ZIPs.
- Move mods between packs and mods root (single or multiple selection).
- Drag and drop selected mods onto destination packs/mods root.
- Create, rename, and delete empty packs.
- Detect duplicate mods across packs.

## Requirements

- Windows (NTFS required for junction support)
- Python 3.10+
- PySide6

Optional:
- `pytest` for running tests

## Installation

```powershell
cd W:\Dany\BeamNG-Manager
python -m pip install PySide6 pytest
```

## Run

```powershell
python -m app.main
```

## First Launch and Settings

Open `File -> Settings...` and configure:
- **BeamNG Mod Folder** (`BeamModsRoot`) 
  - Example: `E:\Mount\NekoNeko101\BeamNG.User\current\mods`
- **Library Root Folder** (`LibraryRoot`)
  - Contains one-level-deep pack folders

Settings are persisted with `QSettings`:
- Organization: `BeamNGManager`
- Application: `ModPackManager`
- Windows location: `HKEY_CURRENT_USER\Software\BeamNGManager\ModPackManager`

## Core Concepts

### Pack activation model

- Active packs are represented by junctions under `BeamModsRoot`.
- Junction name must match pack folder name exactly:
  - `LibraryRoot\career` <-> `BeamModsRoot\career`
- `BeamModsRoot\repo` is treated as repo folder.

### Source scanning

The app scans and caches:
- Pack mods from `LibraryRoot\<pack>` recursively
- Loose mods from `BeamModsRoot` non-recursively
- Repo mods from `BeamModsRoot\repo` recursively
- Unknown junction target mods recursively
- Orphan folder mods recursively

Special directories excluded from orphan/unknown listing:
- `repo`
- `multiplayer`
- `mod_manifests`
- `modconflictresolutions` (case-insensitive)

## UI Overview

### Left panel order

1. `Mods folder`
2. `Mods/Repo folder`
3. `Unknown junction: <name>` entries
4. `Orphan folder: <name>` entries
5. Packs

Pack ordering:
- Enabled packs first (alphabetical)
- Disabled packs second (alphabetical)

### Right panel

Shows ZIP mods for the selected left item.

Columns:
- Filename
- Size
- `info.json` presence indicator

### Status box

- Exactly 3 lines
- Read-only
- No wrapping
- Horizontal scrolling enabled
- Height adapts when horizontal scrollbar appears so line 3 stays visible

## `info.json` Metadata Behavior

- Lazy parsing: metadata is parsed only when selecting a mod row.
- Threaded worker updates status when parsing finishes.
- Caching key: ZIP path + mtime + size.

Selection order inside ZIP:
1. Prefer `mod_info/*/info.json`
2. Else root-level `info.json` if present
3. Else prefer `vehicles/*/info.json`
4. Else shortest path (with alphabetical tie-breaking for same depth)

If multiple `info.json` are at the same level, first alphanumeric path is selected.

Tolerant parser support includes:
- Missing property commas (common malformed JSON)
- Trailing commas before `]` or `}`
- Invalid C0 control character stripping (except TAB/LF/CR)
- Extra trailing closing braces/brackets after a valid root object

Field sets depend on selected `info.json` location:
- `vehicles/*`: `Name, Brand, Author, Country, Body Style, Type, Years, Derby Class, Description, Slogan`
- `levels/*`: `title, authors, size, biome, roads, description, features`
- `mod_info/*`: `title, version_string, prefix_title, username, description, tagline`
- other locations: fallback ordered field matching

In status display, only present fields are shown.

## Pack Management

### Enable / Disable pack

From pack context menu (`left panel`):
- `Enable`
- `Disable`

Rules:
- Refuses operation when `BeamNG.drive.exe` is running.
- Enable checks destination safety:
  - if destination exists and is not junction -> refuse
  - if destination junction points elsewhere -> refuse

### Create pack

Menu: `Packs -> Create pack...`

### Rename pack

- Menu: `Packs -> Rename selected pack...`
- Or right-click pack -> `Rename...`

If pack is active, junction is migrated to new name.

### Delete empty pack

- Menu: `Packs -> Delete selected empty pack...`
- Or right-click pack -> `Delete Empty Pack`

Only empty packs can be deleted.

## Mod Transfer

### Context menu transfer

Right-click mod row(s):
- `Move to pack...`
- `Move to Mods root`

### Multi-selection

- Multiple row selection is supported.
- Batch transfers are supported.
- Status shows: `Mods sélectionnés: X / Affichés: Y` when multiple mods are selected.

### Drag and drop

- Drag selected mod rows from right panel.
- Drop on left panel target:
  - a pack (moves to that pack)
  - `Mods folder` (moves to mods root)

## Duplicate Finder

Menu: `Tools -> Find duplicates...`

Features:
- Group duplicates by normalized filename match (case-insensitive).
- Optional filters:
  - active packs only
  - include loose/repo/orphan/unknown sources

Viewer-only (no auto-delete).

## Junction Implementation Details

- Junction detection uses Windows reparse mount point tag (`IO_REPARSE_TAG_MOUNT_POINT`) via `ctypes`.
- Fallback to `os.readlink` where necessary.
- Junction creation command:
  - `cmd /c mklink /J "<BeamModsRoot>\<pack>" "<LibraryRoot>\<pack>"`
- Junction removal command:
  - `cmd /c rmdir "<BeamModsRoot>\<pack>"`

## Project Structure

```text
app/main.py
ui/main_window.py
ui/settings_dialog.py
ui/duplicates_dialog.py
core/scanner.py
core/junctions.py
core/modinfo.py
core/cache.py
core/actions.py
core/duplicates.py
core/utils.py
tests/
```

## Development

### Run tests

```powershell
python -m pytest -q
```

### Quick compile check

```powershell
python -m compileall app core ui tests
```

## Known Notes

- Junction creation may require elevated privileges depending on local policy/UAC.
- BeamNG path assumptions are **not** hardcoded; settings must be configured explicitly.
- ZIP metadata parsing is best-effort for malformed JSON and will not crash the UI.

## Troubleshooting

### `mklink /J` fails or pack activation does nothing

- Run terminal/app with sufficient permissions (depending on local UAC/policy).
- Verify target filesystem is NTFS.
- Confirm destination `BeamModsRoot\\<pack>` does not already exist as a normal folder.

### Enable/Disable refused while BeamNG is running

- Close `BeamNG.drive.exe` completely.
- Retry activation/deactivation after process exit.

### Rename pack fails for active pack

- Ensure `BeamModsRoot\\<old_pack>` is still a junction to `LibraryRoot\\<old_pack>`.
- Remove conflicting existing path `BeamModsRoot\\<new_pack>` if present.

### Move/transfer fails with “Destination already exists”

- A ZIP with same filename already exists at destination.
- Rename ZIP first or move to another pack.

### `info.json not found` but file exists in ZIP

- File may be malformed JSON beyond current tolerant repair rules.
- Check for severe structural corruption in `info.json`.

### Drag-and-drop does not move mods

- Drop only onto valid left targets:
  - a pack
  - `Mods folder`
- `Mods/Repo`, unknown junctions, and orphan folders are not valid drop targets.

### Special folders appear unexpectedly

- `repo`, `multiplayer`, `mod_manifests`, `modconflictresolutions` are excluded from orphan/unknown listing.
- If one still appears, verify exact folder name and refresh (`File -> Refresh`).
