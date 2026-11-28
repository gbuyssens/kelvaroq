"""F16C instruction results, status flags and guest operand boundaries."""

import os
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from unicorn import (
    UC_ARCH_X86,
    UC_MODE_64,
    UC_PROT_EXEC,
    UC_PROT_READ,
    UC_PROT_WRITE,
    Uc,
)
from unicorn import x86_const as x

from velkin_reference.memory import GuestFault
from velkin_reference.vector import VectorMoves


class HalfTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 4096, UC_PROT_READ | UC_PROT_EXEC)
        self.cpu.mem_map(0x2000, 4096, UC_PROT_READ | UC_PROT_WRITE)
        self.cpu.reg_write(x.UC_X86_REG_EFLAGS, 0x243)
        self.cpu.reg_write(x.UC_X86_REG_MXCSR, 0x1F80)
        self.moves = VectorMoves()
        lib = (
            Path(__file__).resolve().parents[2]
            / "target"
            / "release"
            / "velkin_simd.dll"
        )
        env = patch.dict(
            os.environ,
            {"VELKIN_SIMD_LIB": os.environ.get("VELKIN_SIMD_LIB", str(lib))},
        )
        env.start()
        self.addCleanup(env.stop)

    def step(self, code):
        raw = bytes.fromhex(code)
        self.cpu.mem_write(0x1000, raw)
        try:
            end = self.moves.execute(self.cpu, 0x1000)
        except GuestFault as error:
            if str(error) == "host CPU does not support AVX/F16C":
                self.skipTest(str(error))
            raise
        self.assertEqual(end, 0x1000 + len(raw))
        self.assertEqual(self.cpu.reg_read(x.UC_X86_REG_EFLAGS), 0x243)

    def put(self, reg, data):
        self.cpu.reg_write(reg, int.from_bytes(data, "little"))

    def get(self, reg):
        return self.cpu.reg_read(reg).to_bytes(32, "little")

    def test_explicit_rounding_modes_and_mxcsr_override(self):
        # Above the halfway point, then exactly halfway with an even low neighbor.
        src = struct.pack("<4I", 0x3F801800, 0xBF801800, 0x3F801000, 0xBF801000)
        snan = struct.pack("<4I", *([0x7F800001] * 4))
        self.put(x.UC_X86_REG_YMM6, src + snan)
        up = (0x3C01, 0xBC00, 0x3C01, 0xBC00)
        for imm, initial, halves in (
            (0, 0x1F80, (0x3C01, 0xBC01, 0x3C00, 0xBC00)),
            (1, 0x1F80, (0x3C00, 0xBC01, 0x3C00, 0xBC01)),
            (2, 0x1F80, up),
            (3, 0x1F80, (0x3C00, 0xBC00, 0x3C00, 0xBC00)),
            (0xFF, 0x5F80, up),
        ):
            with self.subTest(imm=imm):
                self.cpu.reg_write(x.UC_X86_REG_MXCSR, initial)
                self.put(x.UC_X86_REG_YMM0, b"\xff" * 32)
                self.step(f"c4 e3 79 1d f0 {imm:02x}")
                self.assertEqual(
                    self.get(x.UC_X86_REG_YMM0), struct.pack("<4H", *halves) + bytes(24)
                )
                self.assertEqual(self.cpu.reg_read(x.UC_X86_REG_MXCSR), initial | 0x20)

    def test_ftz_does_not_flush_half_results_and_daz_controls_input_flags(self):
        src = struct.pack("<4I", 1, 0x80000001, 0x33800000, 0x80000000)
        self.put(x.UC_X86_REG_YMM6, src)
        for control, flags in ((0x1F80, 0x32), (0x9F80, 0x32), (0x9FC0, 0)):
            with self.subTest(mxcsr=control):
                self.cpu.reg_write(x.UC_X86_REG_MXCSR, control)
                self.step("c4 e3 79 1d f0 00")
                self.assertEqual(
                    self.get(x.UC_X86_REG_YMM0),
                    struct.pack("<4H", 0, 0x8000, 1, 0x8000) + bytes(24),
                )
                self.assertEqual(self.cpu.reg_read(x.UC_X86_REG_MXCSR), control | flags)

    def test_half_denormal_and_signaling_nan_widen_with_daz(self):
        src = struct.pack("<4H", 1, 0x8001, 0x7C01, 0xFC00)
        self.put(x.UC_X86_REG_YMM0, src)
        self.put(x.UC_X86_REG_YMM2, b"\xff" * 32)
        self.cpu.reg_write(x.UC_X86_REG_MXCSR, 0x9FC0)
        self.step("c4 e2 79 13 d0")
        expected = struct.pack("<4I", 0x33800000, 0xB3800000, 0x7FC02000, 0xFF800000)
        self.assertEqual(self.get(x.UC_X86_REG_YMM2), expected + bytes(16))
        self.assertEqual(self.cpu.reg_read(x.UC_X86_REG_MXCSR), 0x9FC1)

    def test_xmm_and_ymm_operands_at_last_readable_bytes(self):
        floats = struct.pack("<8f", 1, -2, 3, -4, 5, -6, 7, -8)
        halves = struct.pack(
            "<8H", 0x3C00, 0xC000, 0x4200, 0xC400, 0x4500, 0xC600, 0x4700, 0xC800
        )
        self.put(x.UC_X86_REG_YMM0, floats)
        self.put(x.UC_X86_REG_YMM2, b"\xa5" * 32)
        self.step("c4 e3 7d 1d c2 00")
        self.assertEqual(self.get(x.UC_X86_REG_YMM2), halves + bytes(16))
        for size, store, load in (
            (8, "c4 e3 79 1d 01 00", "c4 e2 79 13 11"),
            (16, "c4 e3 7d 1d 01 00", "c4 e2 7d 13 11"),
        ):
            with self.subTest(size=size):
                address = 0x3000 - size
                self.cpu.reg_write(x.UC_X86_REG_RCX, address)
                self.cpu.mem_write(address - 1, b"\xa5" * (size + 1))
                self.step(store)
                self.assertEqual(
                    self.cpu.mem_read(address - 1, size + 1), b"\xa5" + halves[:size]
                )
                self.step(load)
                self.assertEqual(
                    self.get(x.UC_X86_REG_YMM2), floats[: 2 * size].ljust(32, b"\0")
                )

    def test_faults_preserve_results_and_guest_mxcsr(self):
        old = b"\xcc" * 32
        self.put(x.UC_X86_REG_YMM2, old)
        self.put(x.UC_X86_REG_YMM0, struct.pack("<8I", *([0x7F800001] * 8)))
        self.cpu.reg_write(x.UC_X86_REG_RCX, 0x2FF9)
        self.cpu.mem_write(0x2FF9, b"initial")
        for code in ("c4 e2 79 13 11", "c4 e3 79 1d 01 00"):
            with self.subTest(code=code):
                with self.assertRaises(GuestFault):
                    self.step(code)
                self.assertEqual(self.cpu.reg_read(x.UC_X86_REG_MXCSR), 0x1F80)
                self.assertEqual(self.get(x.UC_X86_REG_YMM2), old)
                self.assertEqual(self.cpu.mem_read(0x2FF9, 7), b"initial")
        self.cpu.reg_write(x.UC_X86_REG_MXCSR, 0)
        with self.assertRaisesRegex(GuestFault, "masked exceptions"):
            self.step("c4 e3 7d 1d c2 00")
        self.assertEqual(self.get(x.UC_X86_REG_YMM2), old)
        self.assertEqual(self.cpu.reg_read(x.UC_X86_REG_MXCSR), 0)
