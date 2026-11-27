"""Guest graphics address space, reservations and CPU-visible committed pages."""

import struct
from dataclasses import dataclass

from unicorn import UC_ERR_NOMEM, UC_PROT_NONE, UC_PROT_READ, UC_PROT_WRITE, UcError
from unicorn.x86_const import (
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RSP,
)

from .memory import DWORD, PAGE, GuestFault, read_guest, write_guest

GRANULARITY = 1 << 16
LARGE_PAGE = 2 << 20
FIRST_HIGH_ADDRESS = 1 << 32
ADDRESS_LIMIT = 1 << 40
RESERVE = 0x2000
COMMIT = 0x1000
PAGES_2M = 0x20000000
PAGES_64K = 0x20400000
CPU_ACCESS = {1: UC_PROT_NONE, 2: UC_PROT_READ, 4: UC_PROT_READ | UC_PROT_WRITE}


@dataclass(frozen=True)
class Reservation:
    start: int
    size: int
    flags: int
    protection: int
    committed: bool
    page_size: int


def aligned(value, alignment):
    return (value + alignment - 1) & -alignment


class GraphicsMemory:
    def __init__(self):
        self.reservations = []
        self.table_pages = None

    def configure(self, cpu):
        address = cpu.reg_read(UC_X86_REG_RCX)
        if not address:
            return 0x80070057  # E_INVALIDARG
        flags, pages, preserved = struct.unpack("<III", read_guest(cpu, address, 12))
        if flags or preserved:
            raise GuestFault("unsupported graphics memory configuration")
        write_guest(
            cpu, address + 16, struct.pack("<QQ", GRANULARITY, ADDRESS_LIMIT - 1)
        )
        self.table_pages = pages
        return 0

    def allocate(self, cpu):
        requested = cpu.reg_read(UC_X86_REG_RCX)
        size = cpu.reg_read(UC_X86_REG_RDX)
        kind = cpu.reg_read(UC_X86_REG_R8) & 0xFFFFFFFF
        flags = cpu.reg_read(UC_X86_REG_R9)
        stack = cpu.reg_read(UC_X86_REG_RSP)
        protection = DWORD.unpack(read_guest(cpu, stack + 40, 4))[0]
        if self.table_pages is None:
            raise GuestFault("graphics aperture is not configured")
        if kind in (PAGES_64K | RESERVE, PAGES_64K | RESERVE | COMMIT):
            page_size, committed = GRANULARITY, bool(kind & COMMIT)
        elif kind == PAGES_2M | RESERVE | COMMIT:
            page_size, committed = LARGE_PAGE, True
        else:
            raise GuestFault(f"unsupported XMem allocation kind {kind:#x}")
        size = aligned(size, PAGE if committed else page_size)
        # The bitness flag selects a low address. For other requests the
        # advertised 40-bit aperture fits even the narrowest console address field.
        low, limit = (
            (GRANULARITY, FIRST_HIGH_ADDRESS)
            if flags & 4
            else (FIRST_HIGH_ADDRESS, ADDRESS_LIMIT)
        )
        address = self.find_space(cpu, requested, size, page_size, low, limit)
        if not address:
            return 0
        if committed:
            access = CPU_ACCESS.get(protection & 0x1FF)
            if access is None:
                raise GuestFault(f"unsupported CPU protection {protection:#x}")
            # Unicorn allocates zero-initialized guest RAM. Keep the original GPU
            # attributes as metadata for resource binding, separate from CPU access.
            try:
                cpu.mem_map(address, size, access)
            except UcError as error:
                if error.errno == UC_ERR_NOMEM:
                    return 0
                raise
        self.reservations.append(
            Reservation(address, size, flags, protection, committed, page_size)
        )
        return address

    def find_space(self, cpu, requested, size, page_size, low, limit):
        candidate = requested or aligned(low, page_size)
        if not size or candidate < low or candidate % page_size:
            return 0
        occupied = [(lo, hi + 1) for lo, hi, _ in cpu.mem_regions()]
        occupied.extend(
            (r.start, r.start + r.size) for r in self.reservations if not r.committed
        )
        for start, end in sorted(occupied):
            if end <= candidate:
                continue
            if candidate + size <= start:
                break
            if requested:
                return 0
            candidate = aligned(end, page_size)
        return candidate if candidate + size <= limit else 0
