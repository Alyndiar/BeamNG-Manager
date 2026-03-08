# BeamNG Mod Pack Manager

Windows-only BeamNG mod pack manager built with PySide6 (Qt6).

The app focuses on local mod organization and active-state management while staying compatible with BeamNG ownership rules.

## Current Scope

### Local tab

- Pack activation/deactivation via NTFS junctions.
- Scan and browse mods from:
  - `mods` root
  - `mods/repo`
  - packs in `LibraryRoot`
  - unknown junction targets
  - orphan folders
- Text and icon views with incremental loading for large folders.
- Prefix/status badges from mod metadata (`info.json` / `prefix_title`), including color-coded badges for common states (`Alpha`, `Beta`, `Experimental`, `Outdated`, `Unsupported`).
- Multi-select enable/disable with progress reporting.
- Move mods between packs and `mods` root.
  - Moving mods to/from `mods/repo` is blocked.
- Drag and drop from mod list to packs or `mods` root (subject to confirmation settings).
- Duplicate finder.

### Profiles

- Profiles store:
  - pack activation states
  - per-mod active states
- Profile load workflow:
  - loads `db.json` active states first
  - compares with profile
  - when conflicts exist, shows per-mod resolution dialog (`Profile` vs `db.json`)
  - default choice is `Profile`; `Cancel` makes `db.json` win
- Profile saving happens only when:
  - explicit Save is used
  - app closes and user chooses save
  - user switches/loads another profile and accepts save prompt
- Save progress is shown as entry counts (`current/total`) in the status box.

### Online tab

- Embedded browsing for BeamNG repository/forums.
- `beamng:v1/...` links are forwarded to BeamNG (no protocol takeover).
- Direct resource downloads are supported.
  - Destination choices: `mods` root or a pack folder.
  - `mods/repo` is intentionally excluded from download destinations.
- Installed-state badges in browser:
  - `Subscribed` (mod present in repo folder)
  - `Manually Installed` (mod present outside repo)

## Safety Rules / Restrictions

- When `BeamNG.drive.exe` is running, file-mutating actions are blocked.
  - Includes pack/mod/profile operations and `db.json` writes.
- BeamNG runtime status is polled by a dedicated worker thread every 15 seconds.
- On app shutdown:
  - pending writes are flushed
  - background operations are awaited before exit
- `db.json` policy:
  - only active-state synchronization for existing managed rows
  - no subscribe/unsubscribe/update logic is performed by this app
  - no repo subscription management

## UI Notes

- App starts maximized.
- Status box is used for long-operation progress.
- BeamNG status indicator square is shown at the right of the status box:
  - red when BeamNG is running
  - empty otherwise
- Confirmation prompts can be globally toggled with `Confirm actions`.
- Checkbox/view preferences are persisted between runs.

## Requirements

- Windows (NTFS required for junctions)
- Python 3.10+ (tested on Python 3.12.x)
- PySide6

Optional:
- `pytest`

## Recommended Python Environment

Using Miniconda is recommended.

Create and use a dedicated environment named `BeamNG-Manager` before installing dependencies, and use this same environment for all program usage (run, tests, and development commands).

```powershell
conda create -n BeamNG-Manager python=3.12 -y
conda activate BeamNG-Manager
```

## Install

```powershell
python -m pip install PySide6 pytest
```

## Run

```powershell
python -m app.main
```

## First Launch

Set paths via `File -> Settings...`:

- `BeamNG Mod Folder` (example: `E:\Mount\NekoNeko101\BeamNG.User\current\mods`)
- `Library Root Folder` (example: `E:\Mount\NekoNeko101\BeamNG.Library`)
- online cache limits

Settings are stored in `QSettings`:

- Organization: `BeamNGManager`
- Application: `ModPackManager`
- Windows registry path: `HKEY_CURRENT_USER\Software\BeamNGManager\ModPackManager`

## Development

### Run tests (recommended command style)

```powershell
conda run -n BeamNG-Manager python -m pytest -q
```

### Quick compile check

```powershell
conda run -n BeamNG-Manager python -m py_compile ui/main_window.py
```

## Project Layout

```text
app/main.py
core/
ui/
tests/
```
