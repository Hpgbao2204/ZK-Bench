"""Platform process counters with explicit unavailable semantics."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProcessCounters:
    supported: bool
    peak_rss_bytes: int | None
    private_bytes: int | None
    page_faults: int | None
    read_bytes: int | None
    write_bytes: int | None
    provider: str
    unavailable_reason: str | None = None


class ProcessCounterProvider(Protocol):
    def capture(self, pid: int) -> ProcessCounters: ...


class UnavailableProcessCounterProvider:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def capture(self, pid: int) -> ProcessCounters:
        del pid
        return ProcessCounters(
            supported=False,
            peak_rss_bytes=None,
            private_bytes=None,
            page_faults=None,
            read_bytes=None,
            write_bytes=None,
            provider="unavailable",
            unavailable_reason=self.reason,
        )


if os.name == "nt":
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


class WindowsProcessCounterProvider:
    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows process counters are unavailable on this platform")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.GetProcessIoCounters.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(IO_COUNTERS),
        ]
        self.kernel32.GetProcessIoCounters.restype = wintypes.BOOL
        self.psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            wintypes.DWORD,
        ]
        self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    @staticmethod
    def _nonboundary(value: int) -> int | None:
        return None if value in (0, 1) else value

    def capture(self, pid: int) -> ProcessCounters:
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return UnavailableProcessCounterProvider(
                f"OpenProcess failed with error {ctypes.get_last_error()}"
            ).capture(pid)
        try:
            memory = PROCESS_MEMORY_COUNTERS_EX()
            memory.cb = ctypes.sizeof(memory)
            io = IO_COUNTERS()
            memory_ok = self.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(memory), memory.cb
            )
            io_ok = self.kernel32.GetProcessIoCounters(handle, ctypes.byref(io))
            if not memory_ok:
                return UnavailableProcessCounterProvider(
                    f"GetProcessMemoryInfo failed with error {ctypes.get_last_error()}"
                ).capture(pid)
            return ProcessCounters(
                supported=True,
                peak_rss_bytes=self._nonboundary(int(memory.PeakWorkingSetSize)),
                private_bytes=self._nonboundary(int(memory.PrivateUsage)),
                page_faults=self._nonboundary(int(memory.PageFaultCount)),
                read_bytes=self._nonboundary(int(io.ReadTransferCount)) if io_ok else None,
                write_bytes=self._nonboundary(int(io.WriteTransferCount)) if io_ok else None,
                provider="windows-psapi",
                unavailable_reason=None if io_ok else "I/O counters unavailable",
            )
        finally:
            self.kernel32.CloseHandle(handle)


def default_process_counter_provider() -> ProcessCounterProvider:
    if os.name == "nt":
        return WindowsProcessCounterProvider()
    return UnavailableProcessCounterProvider("no process counter provider for this platform")
