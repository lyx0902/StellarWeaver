"""Win32 command containment without replacing asyncio's Proactor pipes.

The caller creates the process with CREATE_SUSPENDED. Its primary thread cannot
run user code until this module assigns and verifies a non-breakaway Job Object.
CPython closes CreateProcess's original thread handle, so the still-single-thread
process is identified with the documented ToolHelp APIs. Ambiguity fails closed.

References:
https://learn.microsoft.com/windows/win32/procthread/job-objects
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread
https://learn.microsoft.com/windows/win32/api/tlhelp32/ns-tlhelp32-threadentry32
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
TH32CS_SNAPTHREAD = 0x00000004
ERROR_NO_MORE_FILES = 18
PROCESS_ACCESS = 0x0100 | 0x0001 | 0x1000  # SET_QUOTA | TERMINATE | QUERY_LIMITED_INFORMATION
THREAD_ACCESS = 0x0002 | 0x0800  # SUSPEND_RESUME | QUERY_LIMITED_INFORMATION


def _last_error() -> int:
    if sys.platform != "win32":
        raise OSError("Win32 errors are unavailable on this platform")
    return int(ctypes.get_last_error())


def _windows_error(code: int | None = None) -> OSError:
    if sys.platform != "win32":
        return OSError("Windows Job Object operation is unavailable on this platform")
    return ctypes.WinError(_last_error() if code is None else code)


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class Win32Job:
    """One non-inheritable Job handle owned by one command invocation."""

    def __init__(self) -> None:
        self._kernel: Any = None
        self.handle: int | None = None
        self.assigned: bool = False
        self.resumed: bool = False
        self.primary_thread_id: int | None = None
        if sys.platform != "win32":
            raise OSError("Windows Job Objects are only available on Windows")
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE)
        self._bind(
            "SetInformationJobObject",
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        )
        self._bind(
            "QueryInformationJobObject",
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            wintypes.BOOL,
        )
        self._bind("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL)
        self._bind(
            "IsProcessInJob",
            [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
            wintypes.BOOL,
        )
        self._bind("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL)
        self._bind("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
        self._bind("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE)
        self._bind("Thread32First", [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL)
        self._bind("Thread32Next", [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL)
        self._bind("OpenThread", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
        self._bind("GetProcessIdOfThread", [wintypes.HANDLE], wintypes.DWORD)
        self._bind("ResumeThread", [wintypes.HANDLE], wintypes.DWORD)
        self._bind("CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
        self.handle = self._kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise _windows_error()
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self._kernel.SetInformationJobObject(
                self.handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise _windows_error()
        except BaseException:
            self.close()
            raise

    def _bind(self, name: str, arguments: list[Any], result: Any) -> None:
        function = getattr(self._kernel, name)
        function.argtypes = arguments
        function.restype = result

    def _primary_thread(self, pid: int) -> int:
        snapshot = self._kernel.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise _windows_error()
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            threads: list[int] = []
            success = self._kernel.Thread32First(snapshot, ctypes.byref(entry))
            while success:
                if entry.th32OwnerProcessID == pid:
                    threads.append(int(entry.th32ThreadID))
                entry.dwSize = ctypes.sizeof(entry)
                success = self._kernel.Thread32Next(snapshot, ctypes.byref(entry))
            error = _last_error()
            if error != ERROR_NO_MORE_FILES:
                raise _windows_error(error)
            if len(threads) != 1:
                raise OSError("Cannot uniquely identify the suspended command's primary thread")
            return threads[0]
        finally:
            self._kernel.CloseHandle(snapshot)

    def attach_and_resume(self, pid: int) -> None:
        process = self._kernel.OpenProcess(PROCESS_ACCESS, False, pid)
        if not process:
            raise _windows_error()
        try:
            if not self._kernel.AssignProcessToJobObject(self.handle, process):
                raise _windows_error()
            self.assigned = True
            member = wintypes.BOOL()
            if not self._kernel.IsProcessInJob(process, self.handle, ctypes.byref(member)):
                raise _windows_error()
            if not member.value:
                raise OSError("Command membership in the Windows Job Object was not confirmed")
            thread_id = self._primary_thread(pid)
            thread = self._kernel.OpenThread(THREAD_ACCESS, False, thread_id)
            if not thread:
                raise _windows_error()
            try:
                if self._kernel.GetProcessIdOfThread(thread) != pid:
                    raise OSError("Suspended thread no longer belongs to the owned command")
                previous = int(self._kernel.ResumeThread(thread))
                if previous != 1:
                    if previous == 0xFFFFFFFF:
                        raise _windows_error()
                    raise OSError(
                        "Unexpected primary-thread suspension count; refusing unverified execution"
                    )
                self.primary_thread_id = thread_id
                self.resumed = True
            finally:
                self._kernel.CloseHandle(thread)
        finally:
            self._kernel.CloseHandle(process)

    def accounting(self) -> dict[str, int]:
        data = _Accounting()
        if not self._kernel.QueryInformationJobObject(
            self.handle,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(data),
            ctypes.sizeof(data),
            None,
        ):
            raise _windows_error()
        return {
            "active_processes": int(data.ActiveProcesses),
            "total_processes": int(data.TotalProcesses),
            "terminated_processes": int(data.TotalTerminatedProcesses),
        }

    def contains(self, pid: int) -> bool:
        process = self._kernel.OpenProcess(0x1000, False, pid)
        if not process:
            raise _windows_error()
        try:
            member = wintypes.BOOL()
            if not self._kernel.IsProcessInJob(process, self.handle, ctypes.byref(member)):
                raise _windows_error()
            return bool(member.value)
        finally:
            self._kernel.CloseHandle(process)

    def terminate(self) -> None:
        if self.handle is not None and not self._kernel.TerminateJobObject(self.handle, 1):
            raise _windows_error()

    def close(self) -> None:
        if self.handle is not None:
            handle, self.handle = self.handle, None
            if not self._kernel.CloseHandle(handle):
                raise _windows_error()
