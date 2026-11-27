"""Reservations exclude address ranges without making guest pages accessible."""

import struct
import unittest

from unicorn import UC_ARCH_X86, UC_MODE_64, Uc, UcError
from unicorn.x86_const import (
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RSP,
)

from velkin_reference.graphics_memory import (
    ADDRESS_LIMIT,
    FIRST_HIGH_ADDRESS,
    GRANULARITY,
    GraphicsMemory,
)
from velkin_reference.memory import GuestFault, read_guest, write_guest


class GraphicsMemoryTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 0x1000)
        self.cpu.reg_write(UC_X86_REG_RCX, 0x1000)
        self.cpu.reg_write(UC_X86_REG_RSP, 0x1100)
        self.cpu.mem_write(0x1128, struct.pack("<I", 4))
        self.input = struct.pack("<IIII", 0, 4, 0, 0xAABBCCDD)
        self.cpu.mem_write(0x1000, self.input)
        self.memory = GraphicsMemory()
        self.assertEqual(self.memory.configure(self.cpu), 0)

    def allocate(self, size, base=0, kind=0x20402000, flags=0x21):
        self.cpu.reg_write(UC_X86_REG_RCX, base)
        self.cpu.reg_write(UC_X86_REG_RDX, size)
        self.cpu.reg_write(UC_X86_REG_R8, kind)
        self.cpu.reg_write(UC_X86_REG_R9, flags)
        return self.memory.allocate(self.cpu)

    def test_configuration_writes_only_its_two_outputs(self):
        self.assertEqual(bytes(self.cpu.mem_read(0x1000, 16)), self.input)
        self.assertEqual(
            struct.unpack("<QQ", self.cpu.mem_read(0x1010, 16)),
            (GRANULARITY, ADDRESS_LIMIT - 1),
        )

    def test_large_sparse_reservations_skip_existing_pages(self):
        self.cpu.mem_map(FIRST_HIGH_ADDRESS + GRANULARITY, 4096)
        mappings = list(self.cpu.mem_regions())
        size = 24 << 30
        first = self.allocate(size)
        second = self.allocate(size)
        self.assertEqual(first, FIRST_HIGH_ADDRESS + 2 * GRANULARITY)
        self.assertEqual(second, first + size)
        self.assertEqual(list(self.cpu.mem_regions()), mappings)
        with self.assertRaises(UcError):
            self.cpu.mem_read(first, 1)

    def test_collision_and_overflow_leave_existing_allocations_intact(self):
        first = self.allocate(1)
        self.assertEqual(self.allocate(1, first), 0)
        self.assertEqual(self.allocate(2 * GRANULARITY, ADDRESS_LIMIT - GRANULARITY), 0)
        self.assertEqual(len(self.memory.reservations), 1)
        self.assertEqual(self.memory.reservations[0].size, GRANULARITY)

    def test_large_page_commit_has_distinct_writable_backing(self):
        reserved = self.allocate(GRANULARITY)
        first = self.allocate(0x300000, kind=0x20003000, flags=0x183)
        second = self.allocate(0x1000, kind=0x20003000, flags=0x283)
        self.assertEqual(first % (2 << 20), 0)
        self.assertEqual(second % (2 << 20), 0)
        self.assertGreaterEqual(second, first + 0x300000)
        self.assertEqual(read_guest(self.cpu, first, 4), bytes(4))
        write_guest(self.cpu, first + 0x300000 - 4, b"test")
        self.assertEqual(read_guest(self.cpu, first + 0x300000 - 4, 4), b"test")
        self.assertEqual(read_guest(self.cpu, second, 4), bytes(4))
        with self.assertRaises(GuestFault):
            read_guest(self.cpu, reserved, 1)
        with self.assertRaises(GuestFault):
            read_guest(self.cpu, first + 0x300000, 1)
        with self.assertRaises(UcError):
            self.cpu.emu_start(first, first + 1)

    def test_committed_64k_allocation_preserves_alignment_and_protection(self):
        address = self.allocate(0x20000, kind=0x20403000, flags=0x183)
        region = self.memory.reservations[-1]
        self.assertTrue(region.committed)
        self.assertEqual(region.page_size, GRANULARITY)
        self.assertEqual(address % GRANULARITY, 0)
        self.assertEqual(read_guest(self.cpu, address + 0x1FFFF, 1), b"\0")
        write_guest(self.cpu, address + 0x1FFFF, b"x")
        with self.assertRaises(GuestFault):
            write_guest(self.cpu, address + 0x20000, b"x")

    def test_low_address_request_and_cpu_protection(self):
        self.cpu.mem_write(0x1128, struct.pack("<I", 0x22002))
        address = self.allocate(0x200000, kind=0x20003000, flags=7)
        self.assertGreater(address, 0)
        self.assertLessEqual(address + 0x200000, FIRST_HIGH_ADDRESS)
        self.assertEqual(self.memory.reservations[-1].protection, 0x22002)
        self.assertEqual(read_guest(self.cpu, address, 1), bytes(1))
        with self.assertRaises(GuestFault):
            write_guest(self.cpu, address, b"x")

    def test_unsupported_page_protection_preserves_allocator(self):
        self.cpu.mem_write(0x1128, struct.pack("<I", 0x104))
        mapping = list(self.cpu.mem_regions())
        with self.assertRaises(GuestFault):
            self.allocate(GRANULARITY, kind=0x20003000)
        self.assertEqual(list(self.cpu.mem_regions()), mapping)
        self.assertEqual(self.memory.reservations, [])
