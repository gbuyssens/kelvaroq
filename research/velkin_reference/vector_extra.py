"""Arithmetic and lane selection for the verified VEX operations."""

import ctypes
import os
import struct
from functools import cache

from capstone import x86_const as instructions

from . import fma
from .memory import GuestFault

INT128_OPS = (
    instructions.X86_INS_VPUNPCKHWD,
    instructions.X86_INS_VPSRAD,
    instructions.X86_INS_VSHUFPD,
)

FLOAT128_OPS = {instructions.X86_INS_VCVTDQ2PS: 0, instructions.X86_INS_VSUBPS: 1}

LANE_SELECT_OPS = (
    instructions.X86_INS_VBLENDPS,
    instructions.X86_INS_VBLENDVPS,
    instructions.X86_INS_VINSERTPS,
)

SCALAR_FMA_OPS = {
    getattr(instructions, f"X86_INS_V{family}{order}SS"): 3 * group + index
    for group, family in enumerate(("FMADD", "FMSUB", "FNMADD", "FNMSUB"))
    for index, order in enumerate((132, 213, 231))
}


def scalar_fma(op, dst, second, third, mxcsr):
    # Replicate the active lane so every native lane raises exactly the same
    # sticky exceptions. Inactive source lanes must never take part in a scalar FMA.
    lane, mxcsr = fma.evaluate(op, dst[:4] * 4, second[:4] * 4, third[:4] * 4, mxcsr)
    return lane[:4] + dst[4:16], mxcsr


def select_lanes(op, first, second, control):
    if op == instructions.X86_INS_VBLENDVPS:
        control = sum((high >> 7) << lane for lane, high in enumerate(control[3::4]))
    if op in (instructions.X86_INS_VBLENDPS, instructions.X86_INS_VBLENDVPS):
        return b"".join(
            (second if control & (1 << lane) else first)[4 * lane : 4 * lane + 4]
            for lane in range(len(first) // 4)
        )
    # VINSERTPS selects a register lane with imm[7:6]; an m32 source
    # supplies its only dword regardless of those two bits.
    src = 0 if len(second) == 4 else 4 * ((control >> 6) & 3)
    dst = 4 * ((control >> 4) & 3)
    result = bytearray(first)
    result[dst : dst + 4] = second[src : src + 4]
    for lane in range(4):
        if control & (1 << lane):
            result[4 * lane : 4 * lane + 4] = bytes(4)
    return bytes(result)


def integer_lanes(op, first, second, control=0):
    if op == instructions.X86_INS_VPUNPCKHWD:
        return b"".join(first[i : i + 2] + second[i : i + 2] for i in range(8, 16, 2))
    if op == instructions.X86_INS_VPSRAD:
        count = min(control, 31)
        return struct.pack("<4i", *(v >> count for v in struct.unpack("<4i", first)))
    low = 8 * (control & 1)
    high = 8 * ((control >> 1) & 1)
    return first[low : low + 8] + second[high : high + 8]


class Packed128(ctypes.Structure):
    _fields_ = [
        ("first", ctypes.c_uint32 * 4),
        ("second", ctypes.c_uint32 * 4),
        ("mxcsr", ctypes.c_uint32),
    ]


@cache
def native_packed128(path):
    function = getattr(fma.native_library(path), "velkin_packed128", None)
    if function is None:
        raise GuestFault("packed arithmetic requires the x64 arithmetic backend")
    function.argtypes = [ctypes.c_uint32, ctypes.POINTER(Packed128)]
    function.restype = ctypes.c_uint32
    return function


def packed_float(op, first, second, mxcsr):
    state = Packed128.from_buffer_copy(first + second + mxcsr.to_bytes(4, "little"))
    status = native_packed128(os.environ.get("VELKIN_SIMD_LIB"))(
        op, ctypes.byref(state)
    )
    if status == 3:
        raise GuestFault(
            "guest packed arithmetic requires masked exceptions and valid MXCSR"
        )
    if status:
        raise GuestFault("invalid packed arithmetic operation")
    return bytes(state.first), state.mxcsr
