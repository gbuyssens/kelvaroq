"""Memory widths, result bits and flags for broadcast, scalar FMA and lane selection."""

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
from unicorn.x86_const import (
    UC_X86_REG_EFLAGS,
    UC_X86_REG_MXCSR,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_YMM0,
    UC_X86_REG_YMM1,
    UC_X86_REG_YMM2,
    UC_X86_REG_YMM3,
    UC_X86_REG_YMM4,
)

from velkin_reference import fma
from velkin_reference.memory import GuestFault
from velkin_reference.vector import VectorMoves
from velkin_reference.vector_extra import scalar_fma


class BlockBroadcastTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 0x1000, UC_PROT_EXEC | UC_PROT_READ)
        self.cpu.mem_map(0x2000, 0x1000, UC_PROT_READ | UC_PROT_WRITE)
        self.moves = VectorMoves()

    def broadcast(self, opcode, address):
        # VEX.256.66.0F38 {1A,5A} /r: ymm2, [rip + disp32].
        code = bytes.fromhex("c4 e2 7d") + bytes((opcode, 0x15))
        code += struct.pack("<i", address - 0x1009)
        self.cpu.mem_write(0x1000, code)
        return self.moves.execute(self.cpu, 0x1000)

    def test_only_16_source_bytes_are_required_and_flags_survive(self):
        # Includes a signaling NaN bit pattern. The floating spelling still copies bits.
        payload = struct.pack("<4I", 0x7FA12345, 0x80000000, 0x10325476, 0x98BADCFE)
        self.cpu.mem_write(0x2FF0, payload)
        for opcode in (0x1A, 0x5A):
            with self.subTest(opcode=opcode):
                self.cpu.reg_write(UC_X86_REG_YMM2, (1 << 256) - 1)
                self.cpu.reg_write(UC_X86_REG_MXCSR, 0x1FA2)
                self.cpu.reg_write(UC_X86_REG_EFLAGS, 0x243)
                self.assertEqual(self.broadcast(opcode, 0x2FF0), 0x1009)
                self.assertEqual(
                    self.cpu.reg_read(UC_X86_REG_YMM2).to_bytes(32, "little"),
                    payload * 2,
                )
                self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR), 0x1FA2)
                self.assertEqual(self.cpu.reg_read(UC_X86_REG_EFLAGS), 0x243)

    def test_cross_page_read_fault_preserves_the_whole_destination(self):
        initial = int.from_bytes(bytes(range(32)), "little")
        self.cpu.reg_write(UC_X86_REG_YMM2, initial)
        with self.assertRaises(GuestFault):
            self.broadcast(0x1A, 0x2FF1)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM2), initial)


class ScalarFmaTests(unittest.TestCase):
    def test_native_backend_receives_only_active_lanes(self):
        dst = struct.pack("<4I", 0x3F800001, 0x7FA00001, 0x80000000, 0xDEADBEEF)
        second = struct.pack("<I", 0xBF800000)
        third = struct.pack("<I", 0x3F7FFFFE)
        expected = struct.pack("<I", 0xA8800000)
        with patch.object(
            fma, "evaluate", return_value=(expected * 4, 0x1FA0)
        ) as native:
            result, control = scalar_fma(0, dst, second, third, 0x1F80)
        native.assert_called_once_with(0, dst[:4] * 4, second * 4, third * 4, 0x1F80)
        self.assertEqual(result, expected + dst[4:])
        self.assertEqual(control, 0x1FA0)

    def test_scalar_guest_instruction_at_page_boundary_is_fused(self):
        cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        cpu.mem_map(0x1000, 0x1000, UC_PROT_READ | UC_PROT_EXEC)
        cpu.mem_map(0x2000, 0x1000, UC_PROT_READ | UC_PROT_WRITE)
        # vfmadd132ss xmm0,xmm1,dword ptr [rcx]
        cpu.mem_write(0x1000, bytes.fromhex("c4 e2 71 99 01"))
        cpu.reg_write(UC_X86_REG_RCX, 0x2FFC)
        cpu.mem_write(0x2FFC, struct.pack("<I", 0x3F7FFFFE))
        dst = struct.pack("<4I", 0x3F800001, 0x7FA00001, 0x80000000, 0xDEADBEEF)
        cpu.reg_write(UC_X86_REG_YMM0, int.from_bytes(dst + b"\xff" * 16, "little"))
        cpu.reg_write(
            UC_X86_REG_YMM1,
            int.from_bytes(struct.pack("<4I", 0xBF800000, 0x7FA00001, 0, 0), "little"),
        )
        cpu.reg_write(UC_X86_REG_MXCSR, 0x1F80)
        root = Path(__file__).resolve().parents[2]
        path = os.environ.get(
            "VELKIN_SIMD_LIB", str(root / "target" / "release" / "velkin_simd.dll")
        )
        with patch.dict(os.environ, {"VELKIN_SIMD_LIB": path}):
            try:
                end = VectorMoves().execute(cpu, 0x1000)
            except GuestFault as error:
                if "host CPU does not support" in str(error):
                    self.skipTest(str(error))
                raise
        self.assertEqual(end, 0x1005)
        expected = struct.pack("<I", 0xA8800000) + dst[4:] + bytes(16)
        self.assertEqual(cpu.reg_read(UC_X86_REG_YMM0).to_bytes(32, "little"), expected)
        self.assertEqual(cpu.reg_read(UC_X86_REG_MXCSR), 0x1F80)


class LaneSelectionTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 0x1000, UC_PROT_READ | UC_PROT_EXEC)
        self.cpu.mem_map(0x2000, 0x1000, UC_PROT_READ | UC_PROT_WRITE)
        self.first = (0x7FA12345, 0x80000000, 22, 33, 44, 55, 66, 77)
        self.second = (1, 2, 3, 4, 5, 6, 7, 8)
        for reg, lanes in (
            (UC_X86_REG_YMM0, self.first),
            (UC_X86_REG_YMM1, self.second),
        ):
            self.cpu.reg_write(
                reg, int.from_bytes(struct.pack("<8I", *lanes), "little")
            )
        self.cpu.reg_write(UC_X86_REG_YMM2, (1 << 256) - 1)
        self.cpu.reg_write(UC_X86_REG_MXCSR, 0x1F80)
        self.cpu.reg_write(UC_X86_REG_EFLAGS, 0x243)

    def execute(self, code, destination=UC_X86_REG_YMM2):
        machine_code = bytes.fromhex(code)
        self.cpu.mem_write(0x1000, machine_code)
        self.assertEqual(
            VectorMoves().execute(self.cpu, 0x1000), 0x1000 + len(machine_code)
        )
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_EFLAGS), 0x243)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR), 0x1F80)
        return struct.unpack(
            "<8I", self.cpu.reg_read(destination).to_bytes(32, "little")
        )

    def test_256_bit_blend_uses_independent_masks_for_each_half(self):
        self.assertEqual(
            self.execute("c4 e3 7d 0c d1 a9"), (1, 0x80000000, 22, 4, 44, 6, 66, 8)
        )

    def test_insert_selects_register_lane_then_applies_zero_mask(self):
        self.assertEqual(
            self.execute("c4 e3 79 21 d1 b2"), (0x7FA12345, 0, 22, 3, 0, 0, 0, 0)
        )
        self.assertEqual(
            self.execute("c4 e3 79 21 d1 b8"),
            (0x7FA12345, 0x80000000, 22, 0, 0, 0, 0, 0),
        )

    def test_variable_blend_uses_only_sign_bits_and_handles_destination_alias(self):
        self.cpu.reg_write(UC_X86_REG_YMM2, self.cpu.reg_read(UC_X86_REG_YMM0))
        self.cpu.reg_write(UC_X86_REG_YMM0, self.cpu.reg_read(UC_X86_REG_YMM1))
        mask = struct.pack("<4I", 0x7FFFFFFF, 0x80000001, 0, 0xFFFFFFFF)
        self.cpu.reg_write(UC_X86_REG_YMM1, int.from_bytes(mask, "little"))
        self.assertEqual(
            self.execute("c4 e3 69 4a c0 10", UC_X86_REG_YMM0),
            (0x7FA12345, 2, 22, 4, 0, 0, 0, 0),
        )

    def test_insert_reads_one_dword_and_ignores_source_selector_for_memory(self):
        self.cpu.reg_write(UC_X86_REG_RCX, 0x2FFC)
        self.cpu.mem_write(0x2FFC, struct.pack("<I", 0xCAFEBABE))
        self.assertEqual(
            self.execute("c4 e3 79 21 11 d5"), (0, 0xCAFEBABE, 0, 33, 0, 0, 0, 0)
        )


class Packed128Tests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 0x1000, UC_PROT_READ | UC_PROT_EXEC)
        self.cpu.mem_map(0x2000, 0x1000, UC_PROT_READ | UC_PROT_WRITE)
        self.cpu.reg_write(UC_X86_REG_MXCSR, 0x1F80)
        self.cpu.reg_write(UC_X86_REG_EFLAGS, 0x243)
        root = Path(__file__).resolve().parents[2]
        path = os.environ.get(
            "VELKIN_SIMD_LIB", str(root / "target" / "release" / "velkin_simd.dll")
        )
        library = patch.dict(os.environ, {"VELKIN_SIMD_LIB": path})
        library.start()
        self.addCleanup(library.stop)

    def put(self, reg, value):
        self.cpu.reg_write(reg, int.from_bytes(value + b"\xa5" * 16, "little"))

    def execute(self, code, reg=UC_X86_REG_YMM4):
        machine_code = bytes.fromhex(code)
        self.cpu.mem_write(0x1000, machine_code)
        self.assertEqual(
            VectorMoves().execute(self.cpu, 0x1000), 0x1000 + len(machine_code)
        )
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_EFLAGS), 0x243)
        value = self.cpu.reg_read(reg).to_bytes(32, "little")
        self.assertEqual(value[16:], bytes(16))
        return value[:16]

    def test_unpack_uses_explicit_sources(self):
        self.put(UC_X86_REG_YMM3, struct.pack("<8H", *range(4, 12)))
        self.put(UC_X86_REG_YMM4, b"\xcc" * 16)
        self.assertEqual(
            self.execute("c5 e1 69 e3"), struct.pack("<8H", 8, 8, 9, 9, 10, 10, 11, 11)
        )

    def test_shift_extends_sign_and_saturates_large_counts(self):
        data = struct.pack("<4i", -(2**31), 65535, -1, 2**31 - 1)
        for imm, expected in (("10", (-32768, 0, -1, 32767)), ("ff", (-1, 0, -1, 0))):
            with self.subTest(imm=imm):
                self.put(UC_X86_REG_YMM4, data)
                self.assertEqual(
                    self.execute("c5 d9 72 e4 " + imm), struct.pack("<4i", *expected)
                )

    def test_shuffle_reads_each_encoded_source_before_writing(self):
        self.put(UC_X86_REG_YMM4, struct.pack("<2Q", 0x10, 0x20))
        self.put(UC_X86_REG_YMM3, struct.pack("<2Q", 0x30, 0x40))
        self.assertEqual(self.execute("c5 d9 c6 e3 01"), struct.pack("<2Q", 0x20, 0x30))

    def test_conversion_rounds_up_and_sets_precision_flag(self):
        self.put(UC_X86_REG_YMM4, struct.pack("<4i", 2**24 + 1, -(2**24 + 1), 0, -1))
        self.cpu.reg_write(UC_X86_REG_MXCSR, 0x5F80)
        self.assertEqual(
            self.execute("c5 f8 5b e4"),
            struct.pack("<4I", 0x4B800001, 0xCB800000, 0, 0xBF800000),
        )
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR), 0x5FA0)

    def test_subtract_uses_first_source_and_preserves_registers_on_failure(self):
        first = struct.pack("<4f", 10, 20, 30, 40)
        old = struct.pack("<4f", 100, 110, 120, 130)
        self.put(UC_X86_REG_YMM3, old)
        self.put(UC_X86_REG_YMM4, first)
        self.cpu.mem_write(0x2FF0, struct.pack("<4f", 2, 3, 4, 5))
        # A memory fault and an unsupported unmasked fault policy both leave the destination intact.
        for address, mxcsr in ((0x2FC1, 0x1F80), (0x2FC0, 0)):
            with self.subTest(address=address):
                self.cpu.reg_write(UC_X86_REG_RDX, address)
                self.cpu.reg_write(UC_X86_REG_MXCSR, mxcsr)
                before = self.cpu.reg_read(UC_X86_REG_YMM3)
                with self.assertRaises(GuestFault):
                    self.execute("c5 d8 5c 5a 30", UC_X86_REG_YMM3)
                self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM3), before)
                self.assertEqual(self.cpu.reg_read(UC_X86_REG_MXCSR), mxcsr)
        self.cpu.reg_write(UC_X86_REG_MXCSR, 0x1F80)
        self.assertEqual(
            self.execute("c5 d8 5c 5a 30", UC_X86_REG_YMM3),
            struct.pack("<4f", 8, 17, 26, 35),
        )
