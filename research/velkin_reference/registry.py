"""Process-local, read-only machine registry for the selected console profile."""

import struct

from unicorn.x86_const import (
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RSP,
)

from .memory import DWORD, QWORD, read_guest, read_string, write_guest

MACHINE = 0xFFFFFFFF80000002
OEM = r"software\microsoft\windows nt\currentversion\oem"
CONSOLE_MODES = {"series-x": 0x64, "series-s": 0x60}


class Registry:
    def __init__(self, profile):
        self.values = {OEM: {"consolemode": DWORD.pack(CONSOLE_MODES[profile])}}
        self.handles = {}
        self.next_handle = 0x10000

    def invoke(self, cpu, name):
        handle = cpu.reg_read(UC_X86_REG_RCX)
        if name == "RegCloseKey":
            return 0 if self.handles.pop(handle, None) is not None else 6
        if name == "RegOpenKeyW":
            return self.open(cpu, handle)
        if name == "RegQueryValueExW":
            return self.query(cpu, handle)
        return NotImplemented

    def open(self, cpu, handle):
        if handle == MACHINE:
            parent = ""
        elif handle in self.handles:
            parent = self.handles[handle]
        else:
            return 6  # ERROR_INVALID_HANDLE
        output = cpu.reg_read(UC_X86_REG_R8)
        if not output:
            return 87  # ERROR_INVALID_PARAMETER
        subkey = cpu.reg_read(UC_X86_REG_RDX)
        leaf = read_string(cpu, subkey, True).casefold() if subkey else ""
        if not leaf:
            write_guest(cpu, output, QWORD.pack(handle))
            return 0
        path = (parent + "\\" + leaf).strip("\\")
        if path not in self.values:
            return 2  # ERROR_FILE_NOT_FOUND
        new_handle = self.next_handle
        write_guest(cpu, output, QWORD.pack(new_handle))
        self.handles[new_handle] = path
        self.next_handle += 1
        return 0

    def query(self, cpu, handle):
        if handle not in self.handles:
            return 6
        if cpu.reg_read(UC_X86_REG_R8):
            return 87
        value_name = cpu.reg_read(UC_X86_REG_RDX)
        key = read_string(cpu, value_name, True).casefold() if value_name else ""
        value = self.values[self.handles[handle]].get(key)
        if value is None:
            return 2
        sp = cpu.reg_read(UC_X86_REG_RSP)
        output, length_ptr = struct.unpack("<QQ", read_guest(cpu, sp + 40, 16))
        if output and not length_ptr:
            return 87
        type_ptr = cpu.reg_read(UC_X86_REG_R9)
        if type_ptr:
            write_guest(cpu, type_ptr, DWORD.pack(4))  # REG_DWORD
        capacity = DWORD.unpack(read_guest(cpu, length_ptr, 4))[0] if output else 0
        if length_ptr:
            write_guest(cpu, length_ptr, DWORD.pack(len(value)))
        if output:
            if capacity < len(value):
                return 234  # ERROR_MORE_DATA
            write_guest(cpu, output, value)
        return 0
