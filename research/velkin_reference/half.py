"""VEX conversions between binary16 and binary32 with the host F16C unit."""

import ctypes
import os
from functools import cache

from capstone.x86_const import X86_INS_VCVTPH2PS, X86_INS_VCVTPS2PH
from unicorn.x86_const import UC_X86_REG_MXCSR

from .fma import native_library
from .memory import GuestFault
from .vector_operands import operand_bytes, write_result

HALF_OPS = (X86_INS_VCVTPS2PH, X86_INS_VCVTPH2PS)


class HalfBlock(ctypes.Structure):
    _fields_ = [("lanes", ctypes.c_ubyte * 32), ("mxcsr", ctypes.c_uint32)]


@cache
def native_half(path):
    function = getattr(native_library(path), "velkin_half", None)
    if function is None:
        raise GuestFault("half conversion requires the x64 F16C backend")
    function.argtypes = [ctypes.c_uint32, ctypes.c_uint8, ctypes.POINTER(HalfBlock)]
    function.restype = ctypes.c_uint32
    return function


def execute_half(cpu, insn):
    destination, source, *control = insn.operands
    if insn.id == X86_INS_VCVTPS2PH:
        direction = 0
        width = source.size
        output_width = width // 2
        imm = control[0].imm
    else:
        direction = 1
        output_width = destination.size
        width = output_width // 2
        imm = 0
    payload = operand_bytes(cpu, insn, source, width).ljust(32, b"\x00")
    mxcsr = cpu.reg_read(UC_X86_REG_MXCSR)
    state = HalfBlock.from_buffer_copy(payload + mxcsr.to_bytes(4, "little"))
    convert = native_half(os.environ.get("VELKIN_SIMD_LIB"))
    status = convert(direction, imm, ctypes.byref(state))
    if status == 2:
        raise GuestFault("host CPU does not support AVX/F16C")
    if status == 3:
        raise GuestFault("half conversion requires masked exceptions and valid MXCSR")
    if status:
        raise GuestFault("invalid native half conversion")
    write_result(cpu, insn, destination, bytes(state.lanes[:output_width]))
    cpu.reg_write(UC_X86_REG_MXCSR, state.mxcsr)
