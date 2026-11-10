"""Windows read-only API declarations from qq-nt-export (MIT). See NOTICE.md."""
import ctypes
from ctypes import wintypes as wt
k32=ctypes.WinDLL("kernel32", use_last_error=True)
class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD), ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_ulonglong), ("State", wt.DWORD), ("Protect", wt.DWORD),
        ("Type", wt.DWORD), ("__alignment2", wt.DWORD),
    ]


k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_ulonglong,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_ulonglong, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wt.BOOL
