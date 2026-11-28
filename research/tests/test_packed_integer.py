"""Public instruction-path tests using locally assembled VEX integer fixtures."""

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
from unicorn import x86_const as x

from velkin_reference.memory import GuestFault
from velkin_reference.vector import VectorMoves


class PackedIntegerTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 4096, UC_PROT_READ | UC_PROT_EXEC)
        self.cpu.mem_map(0x2000, 4096, UC_PROT_READ | UC_PROT_WRITE)
        self.cpu.reg_write(x.UC_X86_REG_RCX, 0x2000)
        self.cpu.reg_write(x.UC_X86_REG_EFLAGS, 0x243)
        self.cpu.reg_write(x.UC_X86_REG_MXCSR, 0x1FA2)
        self.moves = VectorMoves()

    def put(self, reg, data):
        self.cpu.reg_write(reg, int.from_bytes(data, "little"))

    def step(self, hex_code):
        code = bytes.fromhex(hex_code)
        self.cpu.mem_write(0x1000, code)
        self.assertEqual(self.moves.execute(self.cpu, 0x1000), 0x1000 + len(code))
        self.assertEqual(self.cpu.reg_read(x.UC_X86_REG_EFLAGS), 0x243)
        self.assertEqual(self.cpu.reg_read(x.UC_X86_REG_MXCSR), 0x1FA2)

    def expect_vector(self, code, expected):
        self.put(x.UC_X86_REG_YMM2, b"\xa5" * 32)
        self.step(code)
        actual = self.cpu.reg_read(x.UC_X86_REG_YMM2).to_bytes(32, "little")
        self.assertEqual(actual, expected.ljust(32, b"\x00"))

    def test_bitwise_register_and_memory_sources(self):
        first = bytes(range(32))
        second = bytes((0xA5 ^ (3 * n)) for n in range(32))
        self.put(x.UC_X86_REG_YMM0, first)
        self.put(x.UC_X86_REG_YMM1, second)
        self.cpu.mem_write(0x2000, second)
        self.expect_vector(
            "c5 f9 ef d1", bytes(a ^ b for a, b in zip(first[:16], second))
        )
        self.expect_vector("c5 fd eb 11", bytes(a | b for a, b in zip(first, second)))

    def test_signed_greater_than_qword(self):
        self.put(x.UC_X86_REG_YMM0, struct.pack("<4q", -(2**63), -1, 2**63 - 1, 5))
        self.put(x.UC_X86_REG_YMM1, struct.pack("<4q", 0, -2, -1, 5))
        self.expect_vector("c4 e2 7d 37 d1", struct.pack("<4q", 0, -1, -1, 0))

    def expect_aliased(self, code, destination, left, right, expected):
        self.put(x.UC_X86_REG_YMM0, left)
        self.put(x.UC_X86_REG_YMM1, right)
        self.step(code)
        actual = self.cpu.reg_read(destination).to_bytes(32, "little")
        self.assertEqual(actual, expected.ljust(32, b"\x00"))

    def test_variable_left_shift_takes_a_count_from_each_dword(self):
        self.expect_aliased(
            "c4 e2 7d 47 c1",
            x.UC_X86_REG_YMM0,
            struct.pack("<8I", 0xFF01, 0x7FFFFFFF, 3, 5, 7, 9, 0xF0000011, 0xC0000003),
            struct.pack("<8I", 0, 1, 31, 32, 33, 0xFFFFFFFF, 4, 2),
            struct.pack("<8I", 0xFF01, 0xFFFFFFFE, 0x80000000, 0, 0, 0, 0x110, 12),
        )

    def test_signed_min_max_compare_opposite_signs_and_endpoints(self):
        left = struct.pack("<8i", -(2**31), 2**31 - 1, -8, 0, 77, -99, 0, -1)
        right = struct.pack("<8i", 2**31 - 1, -(2**31), -7, 0, 7, -199, -1, 0)
        self.expect_aliased(
            "c4 e2 7d 39 c9",
            x.UC_X86_REG_YMM1,
            left,
            right,
            struct.pack("<8i", -(2**31), -(2**31), -8, 0, 7, -199, -1, -1),
        )
        self.expect_aliased(
            "c4 e2 79 3d c1",
            x.UC_X86_REG_YMM0,
            left,
            right,
            struct.pack("<4i", 2**31 - 1, 2**31 - 1, -7, 0),
        )

    def test_pack_signed_dwords_saturates_within_each_128_bit_lane(self):
        self.put(
            x.UC_X86_REG_YMM0,
            struct.pack("<8i", -(2**31), -32768, -1, 0, 12, 32767, 32768, 2**31 - 1),
        )
        self.put(
            x.UC_X86_REG_YMM1,
            struct.pack("<8i", 70000, -32769, 7, 8, -90000, 19, 20, 21),
        )
        low = struct.pack("<8h", -32768, -32768, -1, 0, 32767, -32768, 7, 8)
        high = struct.pack("<8h", 12, 32767, 32767, 32767, -32768, 19, 20, 21)
        self.expect_vector("c5 f9 6b d1", low)
        self.expect_vector("c5 fd 6b d1", low + high)

    def test_pack_signed_words_saturates_within_each_128_bit_lane(self):
        self.put(
            x.UC_X86_REG_YMM0,
            struct.pack(
                "<16h", -32768, -129, -128, -1, 0, 126, 127, 128, *range(20, 28)
            ),
        )
        self.put(
            x.UC_X86_REG_YMM1,
            struct.pack("<16h", *range(-8, 0), 129, 255, 32767, -255, -9, 5, 6, 7),
        )
        low = struct.pack("<16b", -128, -128, -128, -1, 0, 126, 127, 127, *range(-8, 0))
        high = struct.pack("<16b", *range(20, 28), 127, 127, 127, -128, -9, 5, 6, 7)
        self.expect_vector("c5 f9 63 d1", low)
        self.expect_vector("c5 fd 63 d1", low + high)

    def test_shuffles_use_each_lane_and_leave_unselected_half_alone(self):
        self.put(x.UC_X86_REG_YMM0, struct.pack("<16H", *range(16)))
        self.expect_vector(
            "c5 ff 70 d0 1b",
            struct.pack("<16H", 3, 2, 1, 0, 4, 5, 6, 7, 11, 10, 9, 8, 12, 13, 14, 15),
        )
        self.expect_vector(
            "c5 fe 70 d0 b1",
            struct.pack("<16H", 0, 1, 2, 3, 5, 4, 7, 6, 8, 9, 10, 11, 13, 12, 15, 14),
        )
        self.put(x.UC_X86_REG_YMM0, struct.pack("<8I", *range(8)))
        self.expect_vector("c5 fd 70 d0 39", struct.pack("<8I", 1, 2, 3, 0, 5, 6, 7, 4))

    def test_extract_registers_and_memory_mask_the_immediate(self):
        data = bytes(range(0x80, 0xA0))
        self.put(x.UC_X86_REG_YMM0, data)
        self.expect_vector("c4 e3 7d 39 c2 ff", data[16:])
        for reg, code, value in (
            (x.UC_X86_REG_R9, "c4 c3 79 14 c1 1f", 0x8F),
            (x.UC_X86_REG_RAX, "c5 f9 c5 c0 f9", 0x8382),
        ):
            with self.subTest(code=code):
                self.cpu.reg_write(reg, 2**64 - 1)
                self.step(code)
                self.assertEqual(self.cpu.reg_read(reg), value)
        for code, value in (
            ("c4 e3 7d 39 01 fe", data[:16]),
            ("c4 e3 79 14 01 fe", data[14:15]),
            ("c4 e3 79 15 01 fc", data[8:10]),
        ):
            with self.subTest(code=code):
                address = 0x3000 - len(value)
                self.cpu.mem_write(address - 1, b"\xff" * (len(value) + 1))
                self.cpu.reg_write(x.UC_X86_REG_RCX, address)
                self.step(code)
                self.assertEqual(
                    self.cpu.mem_read(address - 1, len(value) + 1), b"\xff" + value
                )

    def test_movd_movq_zero_extend_and_respect_memory_width(self):
        data = bytes(range(0x80, 0xA0))
        self.put(x.UC_X86_REG_YMM0, data)
        r9 = 0xCAFEF00D8ABCDEF0
        for code, width in (("c4 c1 79 6e d1", 4), ("c4 c1 f9 6e d1", 8)):
            with self.subTest(code=code):
                self.cpu.reg_write(x.UC_X86_REG_R9, r9)
                self.expect_vector(code, r9.to_bytes(8, "little")[:width])
        for code, width in (("c4 c1 79 7e c1", 4), ("c4 c1 f9 7e c1", 8)):
            with self.subTest(code=code):
                self.cpu.reg_write(x.UC_X86_REG_R9, 2**64 - 1)
                self.step(code)
                self.assertEqual(
                    self.cpu.reg_read(x.UC_X86_REG_R9),
                    int.from_bytes(data[:width], "little"),
                )
        self.expect_vector("c5 fa 7e d0", data[:8])
        self.cpu.reg_write(x.UC_X86_REG_RCX, 0x2FF8)
        self.step("c5 f9 d6 01")
        self.assertEqual(self.cpu.mem_read(0x2FF8, 8), data[:8])
        self.expect_vector("c5 fa 7e 11", data[:8])

    def test_shift_dword_uses_low_qword_for_register_count(self):
        self.put(
            x.UC_X86_REG_YMM0,
            struct.pack("<8I", 1, 0, 0x80000000, 0xFFFFFFFF, 4, 8, 16, 32),
        )
        expected = struct.pack("<8I", 32, 0, 0, 0xFFFFFFE0, 128, 256, 512, 1024)
        self.expect_vector("c5 ed 72 f0 05", expected)
        self.expect_vector("c5 e9 72 f0 20", bytes(16))
        self.put(x.UC_X86_REG_YMM1, struct.pack("<4Q", 5, 0xDEAD, 0xBEEF, 0xFACE))
        self.expect_vector("c5 fd f2 d1", expected)
        self.put(x.UC_X86_REG_YMM1, struct.pack("<4Q", 2**32 + 5, 0, 0, 0))
        self.expect_vector("c5 fd f2 d1", bytes(32))

    def test_masked_memory_ignores_inactive_lanes_past_page(self):
        values = struct.pack("<8I", 100, 200, 300, 400, 500, 600, 700, 800)
        mask = struct.pack("<8I", 0x80000000, 0x7FFFFFFF, 0xFFFFFFFF, 0, 1, 2, 3, 4)
        self.put(x.UC_X86_REG_YMM0, values)
        self.put(x.UC_X86_REG_YMM1, mask)
        for load, store, width in (
            ("c4 e2 75 8c 11", "c4 e2 75 8e 01", 32),
            ("c4 e2 71 8c 11", "c4 e2 71 8e 01", 16),
        ):
            with self.subTest(width=width):
                self.cpu.reg_write(x.UC_X86_REG_RCX, 0x2FF4)
                self.cpu.mem_write(0x2FF4, struct.pack("<3I", 11, 22, 33))
                self.expect_vector(
                    load, struct.pack("<3I", 11, 0, 33) + bytes(width - 12)
                )
                self.step(store)
                self.assertEqual(
                    self.cpu.mem_read(0x2FF4, 12), struct.pack("<3I", 100, 22, 300)
                )

    def test_masked_load_faults_only_for_an_active_unmapped_lane(self):
        self.cpu.reg_write(x.UC_X86_REG_RCX, 0x3000)
        self.put(x.UC_X86_REG_YMM1, bytes(32))
        self.expect_vector("c4 e2 75 8c 11", bytes(32))
        self.step("c4 e2 75 8e 01")
        self.put(x.UC_X86_REG_YMM1, struct.pack("<8I", 0, 0, 0, 0, 0, 0, 0, 0x80000000))
        before = bytes(range(32))
        self.put(x.UC_X86_REG_YMM2, before)
        with self.assertRaises(GuestFault):
            self.step("c4 e2 75 8c 11")
        self.assertEqual(
            self.cpu.reg_read(x.UC_X86_REG_YMM2).to_bytes(32, "little"), before
        )

    def test_masked_qwords_use_each_64bit_sign_and_skip_unmapped_tail(self):
        self.cpu.reg_write(x.UC_X86_REG_RCX, 0x2FF0)
        self.cpu.mem_write(0x2FF0, struct.pack("<2Q", 10, 20))
        self.put(x.UC_X86_REG_YMM1, struct.pack("<4Q", 1 << 63, 1 << 31, 0, 0))
        self.expect_vector("c4 e2 f5 8c 11", struct.pack("<4Q", 10, 0, 0, 0))
        self.put(x.UC_X86_REG_YMM0, struct.pack("<4Q", 30, 40, 50, 60))
        self.step("c4 e2 f5 8e 01")
        self.assertEqual(
            bytes(self.cpu.mem_read(0x2FF0, 16)), struct.pack("<2Q", 30, 20)
        )
