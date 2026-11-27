"""Exercise the Windows x64 binary32 calling convention through the CRT dispatcher."""

import ctypes
import os
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from unicorn import UC_ARCH_X86, UC_MODE_64, Uc
from unicorn.x86_const import (
    UC_X86_REG_MXCSR,
    UC_X86_REG_XMM0,
    UC_X86_REG_XMM1,
)

from velkin_reference.crt import Crt
from velkin_reference.memory import DWORD, GuestFault


@unittest.skipUnless(os.name == "nt", "requires the Windows UCRT")
class CrtMathTests(unittest.TestCase):
    def setUp(self):
        dll = Path(__file__).resolve().parents[2] / "target/release/velkin_simd.dll"
        backend = patch.dict(os.environ, VELKIN_SIMD_LIB=str(dll))
        backend.start()
        self.addCleanup(backend.stop)
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 4096)
        self.crt = Crt(self.cpu, 0x1000)
        self.errno = self.crt.invoke(self.cpu, "_errno")
        self.cpu.mem_write(self.errno, DWORD.pack(71))

    def call(self, name, first, second=0, mxcsr=0x1F80):
        # Unrelated high lanes ensure the ABI uses precisely one float per argument.
        self.cpu.reg_write(UC_X86_REG_XMM0, (1 << 127) | first)
        self.cpu.reg_write(UC_X86_REG_XMM1, (1 << 90) | second)
        self.cpu.reg_write(UC_X86_REG_MXCSR, mxcsr)
        self.crt.invoke(self.cpu, name)
        return self.cpu.reg_read(UC_X86_REG_XMM0) & 0xFFFFFFFF

    def test_atan2f_uses_y_then_x_and_signed_zero_selects_the_quadrant(self):
        self.assertEqual(self.call("atan2f", 0x80000000, 0xBF800000), 0xC0490FDB)
        self.assertEqual(self.call("atan2f", 0x3F800000, 0), 0x3FC90FDB)
        self.assertEqual(self.call("atan2f", 0x80000000, 0x3F800000), 0x80000000)
        self.assertEqual(self.cpu.mem_read(self.errno, 4), DWORD.pack(71))

    def test_unary_results_and_domain_error_update_guest_state(self):
        self.assertEqual(self.call("sinf", 0x80000000), 0x80000000)
        self.assertEqual(self.call("cosf", 0), 0x3F800000)
        result = self.call("cosf", 0x7F800000, mxcsr=0x3F84)
        self.assertEqual(result & 0x7FC00000, 0x7FC00000)
        self.assertEqual(self.cpu.mem_read(self.errno, 4), DWORD.pack(33))
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR), 0x3F85)
        self.call("sinf", 0)
        self.assertEqual(self.cpu.mem_read(self.errno, 4), DWORD.pack(33))

    def test_unmasked_guest_exception_stops_before_writing_the_result(self):
        with self.assertRaisesRegex(GuestFault, "unmasked exceptions"):
            self.call("sinf", 0x7F800000, mxcsr=0x1F00)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_XMM0), (1 << 127) | 0x7F800000)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR), 0x1F00)
        self.assertEqual(self.cpu.mem_read(self.errno, 4), DWORD.pack(71))

    def test_all_float_imports_agree_with_direct_windows_ucrt(self):
        ucrt = ctypes.CDLL("ucrtbase.dll")
        unary = "tanf cosf sinf exp2f acosf asinf atanf cbrtf ceilf erff expf log10f log2f logf sinhf".split()
        binary = "atan2f fmodf nextafterf powf remainderf".split()
        for names, arguments in [(unary, [0.75]), (binary, [0.75, 0.5])]:
            for name in names:
                with self.subTest(name=name):
                    reference = getattr(ucrt, name)
                    reference.argtypes = [ctypes.c_float] * len(arguments)
                    reference.restype = ctypes.c_float
                    expected = struct.pack("<f", reference(*arguments))
                    actual = self.call(name, 0x3F400000, 0x3F000000)
                    self.assertEqual(actual.to_bytes(4, "little"), expected)
                    self.assertEqual(self.cpu.mem_read(self.errno, 4), DWORD.pack(71))

    def test_exp2f_overflow_log_pole_and_binary_domain(self):
        self.assertEqual(self.call("exp2f", 0x43000000), 0x7F800000)
        self.assertEqual(self.cpu.mem_read(self.errno, 4), DWORD.pack(34))
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR) & 0x28, 0x28)
        self.assertEqual(self.call("logf", 0), 0xFF800000)
        self.assertEqual(self.cpu.mem_read(self.errno, 4), DWORD.pack(34))
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR) & 4, 4)
        self.assertEqual(self.call("fmodf", 0x3F800000, 0) & 0x7FC00000, 0x7FC00000)
        self.assertEqual(self.cpu.mem_read(self.errno, 4), DWORD.pack(33))
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR) & 1, 1)

    def test_binary_argument_and_other_signatures(self):
        self.assertEqual(self.call("nextafterf", 0x3F800000, 0x40000000), 0x3F800001)
        self.assertEqual(self.call("remainderf", 0x40400000, 0x40000000), 0xBF800000)
        for name in ("lroundf", "nexttowardf", "modff", "pow"):
            self.assertIs(self.crt.invoke(self.cpu, name), NotImplemented)
