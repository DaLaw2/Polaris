"""The Recycle Bin, and refusing when something would not arrive there.

`SHFileOperationW` with `FOF_ALLOWUNDO` recycles -- except that an item
larger than its drive's bin allows, or on a drive whose bin is switched
off, is deleted outright, and silently when asked not to confirm. Both are
checked first, so a caller that asked for the bin gets the bin or
`Refused`. Windows only; anywhere else every call is refused.
"""
import ctypes
import sys
from pathlib import Path


class Refused(RuntimeError):
    """Not recycled, and nothing was deleted."""


FO_DELETE = 3
FOF_SILENT = 0x4
FOF_NOCONFIRMATION = 0x10
FOF_ALLOWUNDO = 0x40
FOF_NOERRORUI = 0x400

if sys.platform == "win32":
    from ctypes import wintypes

    class _FileOp(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]


def _limit(path: Path) -> tuple[bool, int]:
    """Whether this drive's bin is switched off, and how many bytes it holds."""
    import winreg

    k32 = ctypes.windll.kernel32
    mount = ctypes.create_unicode_buffer(260)
    volume = ctypes.create_unicode_buffer(64)
    if not (k32.GetVolumePathNameW(str(path), mount, 260)
            and k32.GetVolumeNameForVolumeMountPointW(mount.value, volume, 64)):
        raise Refused(f"cannot tell which drive {path} is on")
    guid = volume.value.removeprefix("\\\\?\\Volume").rstrip("\\")
    key = ("Software\\Microsoft\\Windows\\CurrentVersion\\Explorer"
           f"\\BitBucket\\Volume\\{guid}")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            off = winreg.QueryValueEx(k, "NukeOnDelete")[0]
            mb = winreg.QueryValueEx(k, "MaxCapacity")[0]
    except OSError:
        raise Refused(f"no Recycle Bin settings for {mount.value}; delete one "
                      "file there by hand once and Windows writes them")
    return bool(off), mb * 2**20


def recycle(path: Path, size: int) -> None:
    """Send a file or a folder of `size` bytes to the Recycle Bin."""
    if sys.platform != "win32":
        raise Refused("the Recycle Bin is only reachable on Windows")
    off, room = _limit(path)
    if off:
        raise Refused(f"the Recycle Bin is off for {path.anchor}; "
                      "it would be deleted outright")
    if size > room:
        raise Refused(f"{size >> 20} MB is more than the Recycle Bin on "
                      f"{path.anchor} holds ({room >> 20} MB); "
                      "it would be deleted outright")
    op = _FileOp(wFunc=FO_DELETE, pFrom=str(path) + "\0",
                 fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION
                 | FOF_SILENT | FOF_NOERRORUI)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted:
        raise Refused(f"Windows would not recycle {path} (code {code:#x})")
    if path.exists():
        raise Refused(f"{path} is still there after recycling")
