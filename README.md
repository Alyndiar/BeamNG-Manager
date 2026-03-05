# BeamNG Mod Pack Manager (Windows, PySide6)

## Run

```powershell
pip install PySide6 pytest
python -m app.main
```

## Setup

1. Open `File -> Settings...`.
2. Set `BeamNG Mod Folder` (your BeamNG mods root).
3. Set `Library Root Folder` (contains one-level-deep pack folders).
4. Save. Settings persist via `QSettings` between runs.

## Notes

- Windows NTFS is required for directory junction support.
- Pack activation uses `mklink /J` and deactivation uses `rmdir` on the junction path only.
- Depending on system policy/UAC, creating junctions may require elevated rights.
