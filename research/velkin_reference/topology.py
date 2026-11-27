"""Supply x64 SYSTEM_LOGICAL_PROCESSOR_INFORMATION records to the guest."""

import ctypes
import os
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from unicorn import UC_PROT_WRITE
from unicorn.x86_const import UC_X86_REG_RCX, UC_X86_REG_RDX

from .memory import DWORD, check_access, read_guest

# DWORD Relationship is followed by four alignment bytes before the union.
CORE_RECORD = struct.Struct("<QI4xB15x")
RECORD_SIZE = CORE_RECORD.size
INSUFFICIENT_BUFFER = 122


def manifest_smt_enabled(image: Path, package_root: Path | None = None) -> bool:
    """Read the package-wide Game/VirtualMachine setting, false by GDK default."""
    config_name = "microsoftgame.config"
    directory = package_root if package_root is not None else image.parent
    configs = [path for path in directory.iterdir() if path.name.lower() == config_name]
    if not configs:
        return False
    if len(configs) != 1:
        raise ValueError("ambiguous MicrosoftGame.config next to the executable")
    try:
        game = ET.parse(configs[0]).getroot()
    except ET.ParseError as error:
        raise ValueError(f"invalid MicrosoftGame.config XML: {error}") from error
    namespace, separator, name = game.tag.rpartition("}")
    if name != "Game":
        raise ValueError("expected Game as the root of MicrosoftGame.config")
    prefix = namespace + separator
    machines = game.findall(f"{prefix}VirtualMachine")
    if len(machines) > 1:
        raise ValueError("duplicate VirtualMachine in MicrosoftGame.config")
    if not machines:
        return False
    settings = machines[0].findall(f"{prefix}SMTEnabled")
    if not settings:
        return False
    if len(settings) != 1:
        raise ValueError("duplicate SMTEnabled in MicrosoftGame.config")
    value = (settings[0].text or "").strip()
    if value not in ("true", "1", "false", "0"):
        raise ValueError("invalid SMTEnabled in MicrosoftGame.config")
    return value in ("true", "1")


def console_core_records(smt_enabled):
    # Model the seven title cores only. Adjacent SMT siblings are a model
    # assumption until a matching console VM capture is available.
    width = 2 if smt_enabled else 1
    first_mask = (1 << width) - 1
    return b"".join(
        CORE_RECORD.pack(first_mask << (core * width), 0, int(smt_enabled))
        for core in range(7)
    )


def windows_records():
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("--host-topology requires a 64-bit Windows host")
    query = ctypes.WinDLL(
        "kernel32", use_last_error=True
    ).GetLogicalProcessorInformation
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    query.restype = ctypes.c_int
    size = ctypes.c_uint32()
    buffer = None
    while True:
        if query(buffer, ctypes.byref(size)):
            return bytes(buffer[: size.value])
        error = ctypes.get_last_error()
        if (
            error != INSUFFICIENT_BUFFER
            or not size.value
            or (buffer is not None and size.value <= len(buffer))
        ):
            raise OSError(f"Windows CPU discovery failed with error {error}")
        buffer = ctypes.create_string_buffer(size.value)


class ProcessorInfo:
    def __init__(self, capture=None, *, smt_enabled=False, host_topology=False):
        if capture is not None:
            self.source = "supplied-records"
            records = capture
        elif host_topology:
            self.source = "windows-host"
            records = windows_records()
        else:
            self.source = "console-core-model"
            records = console_core_records(smt_enabled)
        self.load(records)

    def load(self, records):
        if not records or len(records) % RECORD_SIZE:
            raise ValueError("expected complete 32-byte x64 processor records")
        masks = [
            mask
            for mask, relation in struct.iter_unpack("<QI20x", records)
            if relation == 0
        ]
        logical = 0
        for mask in masks:
            if not mask or logical & mask:
                raise ValueError("empty or overlapping core masks in processor records")
            logical |= mask
        if not logical:
            raise ValueError("no core records in processor information")
        self.records = records
        self.summary = {
            "source": self.source,
            "cores": len(masks),
            "logical_processors": logical.bit_count(),
            "core_masks": [hex(mask) for mask in masks],
            "record_count": len(records) // RECORD_SIZE,
        }

    def query(self, cpu):
        output = cpu.reg_read(UC_X86_REG_RCX)
        count_ptr = cpu.reg_read(UC_X86_REG_RDX)
        if not count_ptr:
            return 0, 87
        (capacity,) = DWORD.unpack(read_guest(cpu, count_ptr, 4))
        check_access(cpu, count_ptr, 4, UC_PROT_WRITE)
        required = len(self.records)
        if not output or capacity < required:
            cpu.mem_write(count_ptr, DWORD.pack(required))
            return 0, INSUFFICIENT_BUFFER
        check_access(cpu, output, required, UC_PROT_WRITE)
        cpu.mem_write(output, self.records)
        cpu.mem_write(count_ptr, DWORD.pack(required))
        return 1, None
