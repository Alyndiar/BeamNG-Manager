from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
from pathlib import Path

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
FSCTL_GET_REPARSE_POINT = 0x000900A8
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
OPEN_EXISTING = 3
GENERIC_READ = 0x80000000
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
MAX_REPARSE_SIZE = 16 * 1024

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class REPARSE_DATA_BUFFER(ctypes.Structure):
    _fields_ = [
        ("ReparseTag", wintypes.DWORD),
        ("ReparseDataLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("DataBuffer", ctypes.c_byte * (MAX_REPARSE_SIZE - 8)),
    ]


def _open_reparse_handle(path: Path) -> wintypes.HANDLE:
    handle = _kernel32.CreateFileW(
        str(path),
        GENERIC_READ,
        0,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    return handle


def _query_reparse(path: Path) -> REPARSE_DATA_BUFFER | None:
    handle = _open_reparse_handle(path)
    if handle == INVALID_HANDLE_VALUE:
        return None

    try:
        buf = REPARSE_DATA_BUFFER()
        returned = wintypes.DWORD()
        ok = _kernel32.DeviceIoControl(
            handle,
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            ctypes.byref(buf),
            MAX_REPARSE_SIZE,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            return None
        return buf
    finally:
        _kernel32.CloseHandle(handle)


def is_junction(path: os.PathLike[str] | str) -> bool:
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return False
    buf = _query_reparse(p)
    return bool(buf and buf.ReparseTag == IO_REPARSE_TAG_MOUNT_POINT)


def _decode_mount_target(buf: REPARSE_DATA_BUFFER) -> str | None:
    raw = bytes(buf.DataBuffer)
    if len(raw) < 8:
        return None

    sub_offset = int.from_bytes(raw[0:2], "little")
    sub_len = int.from_bytes(raw[2:4], "little")
    path_bytes = raw[8 + sub_offset : 8 + sub_offset + sub_len]
    if not path_bytes:
        return None

    target = path_bytes.decode("utf-16-le", errors="ignore")
    if target.startswith("\\??\\"):
        target = target[4:]
    return target.rstrip("\\")


def get_junction_target(path: os.PathLike[str] | str) -> Path | None:
    p = Path(path)
    buf = _query_reparse(p)
    if buf and buf.ReparseTag == IO_REPARSE_TAG_MOUNT_POINT:
        target = _decode_mount_target(buf)
        if target:
            return Path(target)

    # Fallback for environments where reparse buffer is blocked
    try:
        target = os.readlink(p)
    except OSError:
        return None
    return Path(target)


def list_junctions(root: os.PathLike[str] | str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    base = Path(root)
    if not base.exists():
        return result

    for child in base.iterdir():
        if not child.is_dir():
            continue
        if not is_junction(child):
            continue
        target = get_junction_target(child)
        if target is not None:
            result[child.name] = target
    return result
