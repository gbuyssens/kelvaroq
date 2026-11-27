"""Waitable kernel objects owned by the single-threaded guest process."""

import struct
from dataclasses import dataclass

from unicorn.x86_const import (
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RSP,
)

from .memory import DWORD, GuestFault, read_guest, write_guest

MODIFY = 0x2
SYNCHRONIZE = 0x100000
WAIT_FAILED = 0xFFFFFFFF


@dataclass
class Semaphore:
    count: int
    maximum: int
    access: int

    def consume(self):
        if self.count == 0:
            return False
        self.count -= 1
        return True


@dataclass
class Event:
    manual_reset: bool
    signaled: bool
    access: int = SYNCHRONIZE | MODIFY

    def consume(self):
        ready = self.signaled
        if not self.manual_reset:
            self.signaled = False
        return ready


@dataclass
class Mutex:
    # The single guest thread can acquire the same mutex recursively.
    depth: int
    access: int = SYNCHRONIZE

    def consume(self):
        self.depth += 1
        return True


class Synchronization:
    def __init__(self):
        self.handles = {}
        self.next_handle = 0x20000

    def insert(self, obj):
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = obj
        return handle, None

    def invoke(self, cpu, name):
        """Return (ABI result, last-error update); None leaves last error unchanged."""
        handle = cpu.reg_read(UC_X86_REG_RCX)
        if name in ("CreateMutexA", "CreateMutexW"):
            if handle or cpu.reg_read(UC_X86_REG_R8):
                raise GuestFault("named or explicitly secured mutex is unsupported")
            owned = bool(cpu.reg_read(UC_X86_REG_RDX) & 0xFFFFFFFF)
            return self.insert(Mutex(int(owned)))
        if name in ("CreateEventA", "CreateEventW"):
            if handle or cpu.reg_read(UC_X86_REG_R9):
                raise GuestFault("named or explicitly secured event is unsupported")
            manual = bool(cpu.reg_read(UC_X86_REG_RDX) & 0xFFFFFFFF)
            signaled = bool(cpu.reg_read(UC_X86_REG_R8) & 0xFFFFFFFF)
            return self.insert(Event(manual, signaled))
        if name == "CreateSemaphoreExW":
            initial = cpu.reg_read(UC_X86_REG_RDX) & 0xFFFFFFFF
            maximum = cpu.reg_read(UC_X86_REG_R8) & 0xFFFFFFFF
            if handle or cpu.reg_read(UC_X86_REG_R9):
                raise GuestFault("named or explicitly secured semaphore is unsupported")
            stack = cpu.reg_read(UC_X86_REG_RSP)
            flags, access = struct.unpack("<I4xI", read_guest(cpu, stack + 40, 12))
            if flags or not 0 < maximum <= 0x7FFFFFFF or initial > maximum:
                return 0, 87
            if access & ~0x1F0003:
                raise GuestFault("unsupported semaphore access rights")
            return self.insert(Semaphore(initial, maximum, access))
        if name not in (
            "ReleaseSemaphore",
            "ReleaseMutex",
            "SetEvent",
            "ResetEvent",
            "WaitForSingleObject",
            "WaitForSingleObjectEx",
            "CloseHandle",
        ):
            return NotImplemented, None
        obj = self.handles.get(handle)
        if obj is None:
            return (WAIT_FAILED if name.startswith("Wait") else 0), 6
        if name == "CloseHandle":
            del self.handles[handle]
            return 1, None
        if name == "ReleaseMutex":
            if not isinstance(obj, Mutex):
                return 0, 6
            if obj.depth == 0:
                return 0, 288  # ERROR_NOT_OWNER
            obj.depth -= 1
            return 1, None
        if name in ("SetEvent", "ResetEvent"):
            if not isinstance(obj, Event):
                return 0, 6
            obj.signaled = name == "SetEvent"
            return 1, None
        if name == "ReleaseSemaphore":
            if not isinstance(obj, Semaphore):
                return 0, 6
            semaphore = obj
            if not semaphore.access & MODIFY:
                return 0, 5
            amount = cpu.reg_read(UC_X86_REG_RDX) & 0xFFFFFFFF
            if not 0 < amount <= 0x7FFFFFFF:
                return 0, 87
            if amount > semaphore.maximum - semaphore.count:
                return 0, 298  # ERROR_TOO_MANY_POSTS
            previous = cpu.reg_read(UC_X86_REG_R8)
            if previous:
                write_guest(cpu, previous, DWORD.pack(semaphore.count))
            semaphore.count += amount
            return 1, None
        if not obj.access & SYNCHRONIZE:
            return WAIT_FAILED, 5
        if name.endswith("Ex") and cpu.reg_read(UC_X86_REG_R8) & 0xFFFFFFFF:
            raise GuestFault("alertable wait requires guest APC delivery")
        if obj.consume():
            return 0, None
        if cpu.reg_read(UC_X86_REG_RDX) & 0xFFFFFFFF:
            raise GuestFault("blocking wait requires guest scheduling")
        return 258, None
