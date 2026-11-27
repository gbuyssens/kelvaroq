"""Test the vector data path with distinctive upper lanes and unaligned memory."""

import struct
import unittest

from unicorn import (
    UC_ARCH_X86,
    UC_MODE_64,
    UC_PROT_EXEC,
    UC_PROT_READ,
    UC_PROT_WRITE,
    Uc,
)
from unicorn.x86_const import (
    UC_X86_REG_RCX,
    UC_X86_REG_YMM0,
    UC_X86_REG_YMM1,
    UC_X86_REG_YMM15,
)

from velkin_reference.memory import GuestFault
from velkin_reference.vector import VectorMoves


class VectorTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 4096, UC_PROT_READ | UC_PROT_EXEC)
        self.cpu.mem_map(0x2000, 4096, UC_PROT_READ | UC_PROT_WRITE)
        self.moves = VectorMoves()
        self.payload = bytes(range(32))
        self.number = int.from_bytes(self.payload, "little")
        self.cpu.reg_write(UC_X86_REG_YMM0, self.number)

    def execute(self, code):
        self.cpu.mem_write(0x1000, bytes.fromhex(code))
        return self.moves.execute(self.cpu, 0x1000)

    def test_unaligned_store_and_rip_relative_load(self):
        self.cpu.reg_write(UC_X86_REG_RCX, 0x2001)
        self.assertEqual(self.execute("c5 fc 11 01"), 0x1004)
        self.assertEqual(bytes(self.cpu.mem_read(0x2001, 32)), self.payload)
        self.assertEqual(self.execute("c5 fc 10 0d f9 0f 00 00"), 0x1008)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM1), self.number)

    def test_vex_128_move_zeroes_upper_half(self):
        self.cpu.reg_write(UC_X86_REG_YMM1, (1 << 256) - 1)
        self.execute("c5 f8 10 c8")
        self.assertEqual(
            self.cpu.reg_read(UC_X86_REG_YMM1), self.number & ((1 << 128) - 1)
        )

    def test_integer_move_preserves_every_bit(self):
        self.cpu.reg_write(UC_X86_REG_RCX, 0x2011)
        self.execute("c5 fe 7f 01")
        self.execute("c5 fe 6f 09")
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM1), self.number)

    def test_zero_upper_preserves_low_lanes(self):
        self.cpu.reg_write(UC_X86_REG_YMM15, self.number)
        self.execute("c5 f8 77")
        for reg in (UC_X86_REG_YMM0, UC_X86_REG_YMM15):
            self.assertEqual(self.cpu.reg_read(reg), self.number & ((1 << 128) - 1))

    def test_store_honours_page_permissions(self):
        self.cpu.reg_write(UC_X86_REG_RCX, 0x1100)
        with self.assertRaises(GuestFault):
            self.execute("c5 fc 11 01")
        self.assertEqual(bytes(self.cpu.mem_read(0x1100, 32)), bytes(32))

    def test_unknown_instruction_stays_with_cpu(self):
        self.assertIsNone(self.execute("0f 0b"))

    def test_qword_subtract_wraps_each_lane(self):
        self.cpu.reg_write(UC_X86_REG_RCX, 0x2000)
        self.cpu.reg_write(
            UC_X86_REG_YMM0, int.from_bytes(struct.pack("<4Q", 0, 4, 3, 1), "little")
        )
        self.cpu.mem_write(0x2000, struct.pack("<4Q", 1, 2, 3, 10))
        self.execute("c5 fd fb 09")
        self.assertEqual(
            self.cpu.reg_read(UC_X86_REG_YMM1).to_bytes(32, "little"),
            struct.pack("<4q", -1, 2, 0, -9),
        )

    def test_packed_equality_compares_each_dword(self):
        data = bytearray(self.payload)
        data[7] ^= 1
        data[31] ^= 1
        self.cpu.mem_write(0x2000, bytes(data))
        self.cpu.reg_write(UC_X86_REG_RCX, 0x2000)
        self.execute("c5 fd 76 09")  # vpcmpeqd ymm1,ymm0,[rcx]
        full = 0xFFFFFFFF
        expected = struct.pack("<8I", full, 0, full, full, full, full, full, 0)
        self.assertEqual(
            self.cpu.reg_read(UC_X86_REG_YMM1).to_bytes(32, "little"), expected
        )
        self.execute("c5 f9 76 c0")  # vpcmpeqd xmm0,xmm0,xmm0
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM0), (1 << 128) - 1)

    def test_sign_extend_bytes_reads_only_the_active_lanes(self):
        self.cpu.reg_write(UC_X86_REG_RCX, 0x2FFC)
        self.cpu.mem_write(0x2FFC, bytes([128, 0, 127, 255]))
        self.execute("c4 e2 7d 22 01")
        self.assertEqual(
            self.cpu.reg_read(UC_X86_REG_YMM0).to_bytes(32, "little"),
            struct.pack("<4q", -128, 0, 127, -1),
        )

    def test_broadcast_reads_exactly_one_qword(self):
        word = 0xABA5123476542211
        self.cpu.mem_write(0x2FF8, struct.pack("<Q", word))
        self.cpu.reg_write(UC_X86_REG_RCX, 0x2FF8)
        self.execute("c4 e2 7d 59 09")
        self.assertEqual(
            self.cpu.reg_read(UC_X86_REG_YMM1).to_bytes(32, "little"),
            struct.pack("<4Q", *[word] * 4),
        )

    def test_lane_add_wraps_and_permutation_crosses_128_bit_halves(self):
        a = struct.pack("<4Q", 0xFFFFFFFFFFFFFFFF, 2, 30, 400)
        b = struct.pack("<4Q", 1, 8, 70, 600)
        self.cpu.reg_write(UC_X86_REG_YMM0, int.from_bytes(a, "little"))
        self.cpu.reg_write(UC_X86_REG_YMM1, int.from_bytes(b, "little"))
        self.execute("c5 fd d4 c1")
        self.execute("c4 e3 fd 00 c8 1b")
        result = self.cpu.reg_read(UC_X86_REG_YMM1).to_bytes(32, "little")
        self.assertEqual(struct.unpack("<4Q", result), (1000, 100, 10, 0))
