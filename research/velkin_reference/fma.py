"""Bridge a fixed set of guest FMA operations to the local x64 arithmetic library."""

import ctypes
import os
from functools import cache

from .memory import GuestFault


class FmaBlock(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32 * 4) for name in ("dst", "second", "third")] + [
        ("mxcsr", ctypes.c_uint32)
    ]


@cache
def native_library(path):
    if not path:
        raise GuestFault(
            "native arithmetic requires the library from the Velkin build"
        )
    try:
        return ctypes.CDLL(path)
    except OSError as error:
        raise GuestFault("cannot load the Velkin arithmetic library") from error


@cache
def native_fma(path):
    function = getattr(native_library(path), "velkin_fma_ps", None)
    if function is None:
        raise GuestFault("FMA requires the x64 arithmetic backend")
    function.argtypes = [ctypes.c_uint32, ctypes.POINTER(FmaBlock)]
    function.restype = ctypes.c_uint32
    return function


def evaluate(op, dst, second, third, mxcsr):
    function = native_fma(os.environ.get("VELKIN_SIMD_LIB"))
    output = bytearray()
    for start in range(0, len(dst), 16):
        raw = b"".join(v[start : start + 16] for v in (dst, second, third))
        block = FmaBlock.from_buffer_copy(raw + mxcsr.to_bytes(4, "little"))
        status = function(op, ctypes.byref(block))
        if status == 2:
            raise GuestFault("host CPU does not support AVX/FMA")
        if status == 3:
            raise GuestFault(
                "guest FMA requires unsupported MXCSR or unmasked exceptions"
            )
        if status:
            raise GuestFault("invalid native FMA operation")
        output.extend(bytes(block.dst))
        mxcsr = block.mxcsr
    return bytes(output), mxcsr


class MathF32(ctypes.Structure):
    _fields_ = [
        ("value", ctypes.c_uint32),
        ("mxcsr", ctypes.c_uint32),
        ("error", ctypes.c_int32),
    ]


CRT_MATH_F32 = {
    "tanf": 0,
    "atan2f": 1,
    "cosf": 2,
    "sinf": 3,
    "exp2f": 4,
    "acosf": 5,
    "asinf": 6,
    "atanf": 7,
    "cbrtf": 8,
    "ceilf": 9,
    "erff": 10,
    "expf": 11,
    "fmodf": 12,
    "log10f": 13,
    "log2f": 14,
    "logf": 15,
    "nextafterf": 16,
    "powf": 17,
    "remainderf": 18,
    "sinhf": 19,
}


@cache
def native_math_f32(path):
    function = getattr(native_library(path), "velkin_math_f32", None)
    if function is None:
        raise GuestFault("CRT math requires the x64 Windows math backend")
    function.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(MathF32)]
    function.restype = ctypes.c_uint32
    return function


def math_f32(name, value, second, mxcsr):
    state = MathF32(value, mxcsr, 0)
    function = native_math_f32(os.environ.get("VELKIN_SIMD_LIB"))
    status = function(CRT_MATH_F32[name], second, ctypes.byref(state))
    if status == 3:
        raise GuestFault(
            f"guest {name} requires unsupported MXCSR or unmasked exceptions"
        )
    if status:
        raise GuestFault(f"native CRT backend rejected {name} (status {status})")
    return state


def tanf(value, mxcsr):
    return math_f32("tanf", value, 0, mxcsr)
