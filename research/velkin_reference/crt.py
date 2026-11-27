"""Single-process CRT state and startup services."""

from unicorn import UC_PROT_READ, UC_PROT_WRITE
from unicorn.x86_const import (
    UC_X86_REG_MXCSR,
    UC_X86_REG_R8,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_XMM0,
    UC_X86_REG_XMM1,
)

from .fma import CRT_MATH_F32, math_f32
from .memory import DWORD, QWORD, GuestFault, check_access, read_guest, write_guest


def fill_memory(cpu):
    dst = cpu.reg_read(UC_X86_REG_RCX)
    value = cpu.reg_read(UC_X86_REG_RDX) & 255
    count = cpu.reg_read(UC_X86_REG_R8)
    if count > 64 * 1024**2:
        raise GuestFault("memset exceeds the reference service limit")
    check_access(cpu, dst, count, UC_PROT_WRITE)
    chunk = bytes([value]) * min(count, 65536)
    for offset in range(0, count, 65536):
        cpu.mem_write(dst + offset, chunk[: count - offset])
    return dst


def copy_memory(cpu):
    dst = cpu.reg_read(UC_X86_REG_RCX)
    src = cpu.reg_read(UC_X86_REG_RDX)
    count = cpu.reg_read(UC_X86_REG_R8)
    check_access(cpu, src, count, UC_PROT_READ)
    check_access(cpu, dst, count, UC_PROT_WRITE)
    if dst == src or not count:
        return dst
    # Offer memmove behavior for overlap. Each temporary holds at most 64 KiB.
    backwards = src < dst < src + count
    for copied in range(0, count, 65536):
        width = min(65536, count - copied)
        offset = count - copied - width if backwards else copied
        data = bytes(cpu.mem_read(src + offset, width))
        cpu.mem_write(dst + offset, data)
    return dst


def string_length(cpu, address):
    offset = 0
    while offset < 16 * 1024**2:
        width = 4096 - ((address + offset) % 4096)
        page = read_guest(cpu, address + offset, width)
        terminator = page.find(b"\0")
        if terminator >= 0:
            return offset + terminator
        offset += width
    raise GuestFault("string exceeds reference service limit")


def compare_strings(cpu, left, right, count=None, *, ignore_case=False):
    # Compare unsigned bytes in the startup C locale. An equal NUL ends both strings.
    limit = 16 * 1024**2
    for index in range(min(count, limit) if count is not None else limit):
        a = read_guest(cpu, left + index, 1)
        b = read_guest(cpu, right + index, 1)
        if ignore_case:
            a, b = a.lower(), b.lower()
        a, b = a[0], b[0]
        if a != b or not a:
            return (a - b) & 0xFFFFFFFF
    if count is None or count > limit:
        raise GuestFault("string comparison exceeds reference service limit")
    return 0


class Crt:
    def __init__(self, cpu, base):
        self.crt = base
        self.app_type = 0
        self.fmode = 0x4000
        self.locale_mode = 2
        self.argv_mode = 0
        self.math_handler = 0
        self.new_mode = 0
        self.narrow_environment = False
        self.onexit = {}
        self.exit_callbacks = []
        # One virtual executable and an empty environment. Guest pointers
        # stay valid for the lifetime of this startup process.
        path = "T:\\title.exe\0"
        self.command_lines = {
            "GetCommandLineA": base + 0x100,
            "GetCommandLineW": base + 0x200,
        }
        cpu.mem_write(base + 0x100, path.encode("ascii"))
        cpu.mem_write(base + 0x200, path.encode("utf-16-le"))
        cpu.mem_write(self.crt + 0x80, QWORD.pack(self.crt + 0x100) + QWORD.pack(0))
        cpu.mem_write(self.crt + 0x10, DWORD.pack(1))
        cpu.mem_write(self.crt + 0x18, QWORD.pack(self.crt + 0x80))

    def invoke(self, cpu, name):
        arg = cpu.reg_read(UC_X86_REG_RCX)
        result = 0
        if name in CRT_MATH_F32:
            bits = cpu.reg_read(UC_X86_REG_XMM0) & 0xFFFFFFFF
            # The typed UCRT entry consumes XMM1 only for binary operations.
            second = cpu.reg_read(UC_X86_REG_XMM1) & 0xFFFFFFFF
            state = math_f32(name, bits, second, cpu.reg_read(UC_X86_REG_MXCSR))
            if state.error:
                if self.math_handler:
                    raise GuestFault(
                        "guest math error handler invocation is unsupported"
                    )
                write_guest(cpu, self.crt + 0x20, DWORD.pack(state.error))
            cpu.reg_write(UC_X86_REG_XMM0, state.value)
            cpu.reg_write(UC_X86_REG_MXCSR, state.mxcsr)
            return 0
        if name == "_errno":
            return self.crt + 0x20
        if name == "strlen":
            return string_length(cpu, arg)
        if name in ("strcmp", "_strnicmp"):
            limited = name == "_strnicmp"
            return compare_strings(
                cpu,
                arg,
                cpu.reg_read(UC_X86_REG_RDX),
                cpu.reg_read(UC_X86_REG_R8) if limited else None,
                ignore_case=limited,
            )
        if name == "_set_app_type":
            self.app_type = arg
        elif name == "_set_new_mode":
            mode = arg & 0xFFFFFFFF
            if mode not in (0, 1):
                raise GuestFault(
                    "invalid CRT new mode requires invalid-parameter handling"
                )
            result = self.new_mode
            self.new_mode = mode
        elif name == "_set_fmode":
            if arg in (0x4000, 0x8000, 0x10000):
                self.fmode = arg
            else:
                result = 22
        elif name == "__p__commode":
            result = self.crt
        elif name == "_configure_narrow_argv":
            if arg in (0, 1, 2):
                self.argv_mode = arg
            else:
                result = 22
        elif name == "__p___argc":
            result = self.crt + 0x10
        elif name == "__p___argv":
            result = self.crt + 0x18
        elif name == "_configthreadlocale":
            result = self.locale_mode
            if arg in (1, 2):
                self.locale_mode = arg
            elif arg != 0:
                result = 0xFFFFFFFF
        elif name == "_initialize_narrow_environment":
            self.narrow_environment = True
        elif name == "__setusermatherr":
            self.math_handler = arg
        elif name == "_crt_atexit":
            if arg:
                self.exit_callbacks.append(arg)
            else:
                result = 0xFFFFFFFF
        elif name == "_initialize_onexit_table":
            if arg:
                write_guest(cpu, arg, bytes(24))
                self.onexit.setdefault(arg, [])
            else:
                result = 0xFFFFFFFF
        else:
            return NotImplemented
        return result
