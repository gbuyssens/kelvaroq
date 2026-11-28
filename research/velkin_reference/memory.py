"""PE pages and initial Windows x64 thread layout inside Unicorn."""

import hashlib
import struct

from unicorn import UC_PROT_EXEC, UC_PROT_READ, UC_PROT_WRITE
from unicorn.x86_const import UC_X86_REG_GS_BASE

PAGE = 4096
QWORD = struct.Struct("<Q")
DWORD = struct.Struct("<I")
STACK_SIZE = 1024 * 1024
RW = UC_PROT_READ | UC_PROT_WRITE
RX = UC_PROT_READ | UC_PROT_EXEC


def align(value):
    return (value + PAGE - 1) & -PAGE


class GuestFault(Exception):
    """An invalid operation requested by the guest."""


def check_access(cpu, start, size, permission):
    """Validate the whole span before an HLE read or write bypasses Unicorn's MMU."""
    if size == 0:
        return
    if not 0 <= start < 2**64 or not 0 < size <= 2**64 - start:
        raise GuestFault("invalid guest address range")
    cursor = start
    for low, high, flags in sorted(cpu.mem_regions()):
        if high < cursor:
            continue
        if low > cursor or not flags & permission:
            break
        cursor = high + 1
        if cursor >= start + size:
            return
    raise GuestFault(f"guest access violation at {cursor:#x}")


def write_guest(cpu, address, data):
    check_access(cpu, address, len(data), UC_PROT_WRITE)
    cpu.mem_write(address, data)


def read_guest(cpu, address, size):
    check_access(cpu, address, size, UC_PROT_READ)
    return bytes(cpu.mem_read(address, size))


def read_string(cpu, address, wide):
    width = 2 if wide else 1
    data = bytearray()
    for offset in range(0, 65536, width):
        unit = read_guest(cpu, address + offset, width)
        if not any(unit):
            return data.decode("utf-16-le" if wide else "ascii", errors="replace")
        data.extend(unit)
    raise GuestFault("unterminated guest string")


def map_image(cpu, plan, data):
    if hashlib.sha256(data).hexdigest() != plan["sha256"]:
        raise ValueError("image changed after inspection")
    base, size = plan["base"], plan["size"]
    if size > 1024**3 or not size or base % PAGE:
        raise ValueError("image outside reference CPU limits")
    cpu.mem_map(base, align(plan["headers"]), UC_PROT_READ)
    cpu.mem_write(base, data[: plan["headers"]])
    for s in plan["sections"]:
        length = max(s["virtual_size"], s["raw_size"])
        if not length:
            continue
        flags = s["flags"]
        protection = (
            (UC_PROT_READ if flags & 0x40000000 else 0)
            | (UC_PROT_WRITE if flags & 0x80000000 else 0)
            | (UC_PROT_EXEC if flags & 0x20000000 else 0)
        )
        at = base + s["rva"]
        cpu.mem_map(at, align(length), protection)
        if s["raw_size"]:
            cpu.mem_write(at, data[s["offset"] : s["offset"] + s["raw_size"]])


def prepare_services(cpu, plan):
    # Keep all emulated services, thread state and stack outside the PE.
    base, size = plan["base"], plan["size"]
    base_services = align(base + size) + PAGE
    symbols = list(plan["imports"])
    sync_dll = "api-ms-win-core-synch-l1-2-0.dll"
    for service in ("SleepConditionVariableCS", "WakeAllConditionVariable"):
        symbols.append({"dll": sync_dll, "name": service})
    service_bytes = align((len(symbols) + 1) * 16)
    cpu.mem_map(base_services, service_bytes, RX)
    return_pc = base_services + 16 * len(symbols)
    for n, symbol in enumerate(plan["imports"]):
        at = symbol["iat"]
        if at < 0 or at + 8 > size:
            raise ValueError("IAT slot outside image")
        cpu.mem_write(base + at, QWORD.pack(base_services + 16 * n))
    cpu.mem_write(base_services, b"\xc3" * service_bytes)
    return symbols, base_services, return_pc, base_services + service_bytes + PAGE


def prepare_thread(cpu, plan, thread):
    base, size = plan["base"], plan["size"]
    cpu.mem_map(thread, 4 * PAGE, RW)
    cpu.reg_write(UC_X86_REG_GS_BASE, thread)
    slots, peb = thread + PAGE, thread + 2 * PAGE
    template = thread + 5 * PAGE
    cpu.mem_write(thread + 0x30, QWORD.pack(thread))
    cpu.mem_write(thread + 0x58, QWORD.pack(slots))
    cpu.mem_write(thread + 0x60, QWORD.pack(peb))
    cpu.mem_write(peb + 0x10, QWORD.pack(base))
    tls = plan["tls"]
    tls_size = PAGE
    if tls:
        start, end = tls["start"], tls["end"]
        if (start or end) and not (base <= start <= end <= base + size):
            raise ValueError("invalid TLS template")
        if not (base <= tls["index"] <= base + size - 4):
            raise ValueError("invalid TLS index")
        tls_size = align(max(1, end - start + tls["zero_fill"]))
        if tls_size > 16 * 1024**2:
            raise ValueError("TLS exceeds reference CPU limits")
        cpu.mem_map(template, tls_size, RW)
        if end > start:
            cpu.mem_write(template, bytes(cpu.mem_read(start, end - start)))
        cpu.mem_write(tls["index"], DWORD.pack(0))
        cpu.mem_write(slots, QWORD.pack(template))
    stack = template + tls_size + PAGE
    cpu.mem_map(stack, STACK_SIZE, RW)
    stack_top = stack + STACK_SIZE
    cpu.mem_write(thread + 8, QWORD.pack(stack_top))
    cpu.mem_write(thread + 0x10, QWORD.pack(stack))
    return stack_top, thread + 3 * PAGE
