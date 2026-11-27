"""Kernel services with a single virtual thread and a deterministic clock."""

import struct

from unicorn.x86_const import UC_X86_REG_RCX, UC_X86_REG_RDX

from .memory import QWORD, GuestFault, read_string, write_guest
from .synchronization import Synchronization
from .topology import ProcessorInfo


class Kernel:
    def __init__(
        self,
        base,
        symbols,
        services,
        command_lines,
        processor_info=None,
        *,
        smt_enabled=False,
        host_topology=False,
    ):
        self.base = base
        self.command_lines = command_lines
        self.symbols = symbols
        self.services = services
        self.clock = 0
        self.last_error = 0
        self.unhandled_exception_filter = 0
        self.critical_sections = {}
        self.synchronization = Synchronization()
        self.processor_info = ProcessorInfo(
            processor_info, smt_enabled=smt_enabled, host_topology=host_topology
        )
        self.modules = {
            dll: 0x4000 + i
            for i, dll in enumerate(sorted({s["dll"] for s in self.symbols}))
        }
        self.modules.setdefault("kernel32.dll", 0x3000)

    def invoke(self, cpu, name):
        arg = cpu.reg_read(UC_X86_REG_RCX)
        result = 0
        if name in (
            "InitializeCriticalSectionAndSpinCount",
            "InitializeCriticalSectionEx",
            "InitializeCriticalSection",
        ):
            spin = (
                cpu.reg_read(UC_X86_REG_RDX) & 0xFFFFFFFF
                if name != "InitializeCriticalSection"
                else 0
            )
            self.critical_sections[arg] = 0
            write_guest(cpu, arg, struct.pack("<QiiQQQ", 0, -1, 0, 0, 0, spin))
            result = 1
        elif name in (
            "EnterCriticalSection",
            "TryEnterCriticalSection",
            "LeaveCriticalSection",
            "DeleteCriticalSection",
        ):
            depth = self.critical_sections.get(arg)
            if depth is None or (name == "LeaveCriticalSection" and depth == 0):
                raise GuestFault("invalid critical section operation")
            if name == "DeleteCriticalSection":
                del self.critical_sections[arg]
            else:
                depth += -1 if name == "LeaveCriticalSection" else 1
                self.critical_sections[arg] = depth
                write_guest(
                    cpu,
                    arg + 8,
                    struct.pack("<iiQ", depth - 1, depth, 0x104 if depth else 0),
                )
                result = 1
        elif name in ("GetModuleHandleW", "GetModuleHandleA"):
            result = (
                self.base
                if not arg
                else self.modules.get(
                    read_string(cpu, arg, name.endswith("W")).lower(), 0
                )
            )
            if not result:
                self.last_error = 126
        elif name == "GetProcAddress":
            pointer = cpu.reg_read(UC_X86_REG_RDX)
            requested = (
                read_string(cpu, pointer, False)
                if pointer > 65535
                else f"ORDINAL {pointer}"
            )
            for slot, item in enumerate(self.symbols):
                if self.modules.get(item["dll"]) == arg and item["name"] == requested:
                    result = self.services + 16 * slot
                    break
            if not result:
                self.last_error = 127
        elif name == "WakeAllConditionVariable":
            # The reference scheduler has a single active thread.
            pass
        elif name == "InitializeSListHead":
            if arg % 16:
                raise GuestFault("unaligned SLIST_HEADER")
            write_guest(cpu, arg, bytes(16))
        elif name in ("GetCurrentProcessId", "GetCurrentThreadId"):
            result = 0x100 if name == "GetCurrentProcessId" else 0x104
        elif name in ("QueryPerformanceCounter", "QueryPerformanceFrequency"):
            self.clock += 1
            write_guest(
                cpu,
                arg,
                QWORD.pack(self.clock if name.endswith("Counter") else 10_000_000),
            )
            result = 1
        elif name in ("GetSystemTimeAsFileTime", "GetSystemTimePreciseAsFileTime"):
            write_guest(cpu, arg, QWORD.pack(133000000000000000 + self.clock))
        elif name in self.command_lines:
            result = self.command_lines[name]
        elif name == "SetUnhandledExceptionFilter":
            result = self.unhandled_exception_filter
            self.unhandled_exception_filter = arg
        elif name == "GetLogicalProcessorInformation":
            result, error = self.processor_info.query(cpu)
            if error is not None:
                self.last_error = error
        elif name == "GetLastError":
            result = self.last_error
        elif name == "SetLastError":
            self.last_error = arg & 0xFFFFFFFF
        else:
            result, error = self.synchronization.invoke(cpu, name)
            if error is not None:
                self.last_error = error
        return result
