"""Platform process counters with explicit unavailable semantics."""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ProcessCounters:
    supported: bool
    peak_rss_bytes: int | None
    private_bytes: int | None
    page_faults: int | None
    read_bytes: int | None
    write_bytes: int | None
    swap_bytes: int | None
    cpu_time_ns: int | None
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
            swap_bytes=None,
            cpu_time_ns=None,
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
        self.kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        self.kernel32.GetProcessTimes.restype = wintypes.BOOL
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
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            memory_ok = self.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(memory), memory.cb
            )
            io_ok = self.kernel32.GetProcessIoCounters(handle, ctypes.byref(io))
            times_ok = self.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
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
                swap_bytes=None,
                cpu_time_ns=(
                    self._nonboundary(
                        (
                            (int(kernel.dwHighDateTime) << 32)
                            + int(kernel.dwLowDateTime)
                            + (int(user.dwHighDateTime) << 32)
                            + int(user.dwLowDateTime)
                        )
                        * 100
                    )
                    if times_ok
                    else None
                ),
                provider="windows-psapi",
                unavailable_reason=(
                    "per-process swap allocation unavailable"
                    if io_ok and times_ok
                    else "some I/O, CPU-time, or swap counters are unavailable"
                ),
            )
        finally:
            self.kernel32.CloseHandle(handle)


def _nonboundary(value: int | None) -> int | None:
    return None if value is None or value in (0, 1) else value


def _parse_proc_key_values(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        fields = raw.strip().split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue
        if len(fields) > 1 and fields[1].lower() == "kb":
            value *= 1024
        values[key] = value
    return values


def _parse_proc_stat(text: str) -> tuple[int, int]:
    closing = text.rfind(")")
    if closing < 0:
        raise ValueError("malformed /proc stat: missing process-name terminator")
    fields_after_name = text[closing + 1 :].split()
    if len(fields_after_name) <= 12:
        raise ValueError("malformed /proc stat: missing page-fault/CPU fields")
    # fields_after_name[0] is field 3 (state). Page faults are fields 10/12,
    # while user/system CPU ticks are fields 14/15.
    faults = int(fields_after_name[7]) + int(fields_after_name[9])
    cpu_ticks = int(fields_after_name[11]) + int(fields_after_name[12])
    return faults, cpu_ticks


class LinuxProcProcessCounterProvider:
    """Read process counters without psutil or a global package install."""

    def __init__(
        self,
        proc_root: Path = Path("/proc"),
        clock_ticks_per_second: int | None = None,
    ) -> None:
        self.proc_root = proc_root
        self.clock_ticks_per_second = (
            clock_ticks_per_second
            if clock_ticks_per_second is not None
            else int(os.sysconf("SC_CLK_TCK"))
        )

    def capture(self, pid: int) -> ProcessCounters:
        process_root = self.proc_root / str(pid)
        try:
            status = _parse_proc_key_values(
                (process_root / "status").read_text(encoding="utf-8")
            )
            io_values = _parse_proc_key_values(
                (process_root / "io").read_text(encoding="utf-8")
            )
            page_faults, cpu_ticks = _parse_proc_stat(
                (process_root / "stat").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            return UnavailableProcessCounterProvider(
                f"Linux procfs read failed: {error}"
            ).capture(pid)
        return ProcessCounters(
            supported=True,
            peak_rss_bytes=_nonboundary(status.get("VmHWM")),
            # RssAnon is not equivalent to Windows private commit; leave it blank.
            private_bytes=None,
            page_faults=_nonboundary(page_faults),
            read_bytes=_nonboundary(io_values.get("read_bytes")),
            write_bytes=_nonboundary(io_values.get("write_bytes")),
            swap_bytes=_nonboundary(status.get("VmSwap")),
            cpu_time_ns=_nonboundary(
                (cpu_ticks * 1_000_000_000) // self.clock_ticks_per_second
            ),
            provider="linux-procfs",
            unavailable_reason=(
                "private commit and per-process swap I/O are unavailable; "
                "VmSwap reports allocation, not swap traffic"
            ),
        )


def default_process_counter_provider() -> ProcessCounterProvider:
    if os.name == "nt":
        return WindowsProcessCounterProvider()
    if sys.platform.startswith("linux"):
        return LinuxProcProcessCounterProvider()
    return UnavailableProcessCounterProvider("no process counter provider for this platform")
