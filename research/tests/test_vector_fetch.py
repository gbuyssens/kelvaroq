"""Exercise repeated VEX fetches, live instruction writes and page permissions."""

import struct
import unittest
from types import SimpleNamespace

from unicorn import (
    UC_ARCH_X86,
    UC_ERR_FETCH_PROT,
    UC_HOOK_CODE,
    UC_MODE_64,
    UC_PROT_EXEC,
    UC_PROT_READ,
    UC_PROT_WRITE,
    Uc,
    UcError,
)
from unicorn.x86_const import UC_X86_REG_RIP, UC_X86_REG_YMM0, UC_X86_REG_YMM1

from velkin_reference.cpu import Startup
from velkin_reference.vector import VectorMoves


class VectorFetchTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 4096, UC_PROT_READ | UC_PROT_WRITE | UC_PROT_EXEC)
        self.vm = SimpleNamespace(vector=VectorMoves(), blocker=None)
        self.cpu.hook_add(UC_HOOK_CODE, self.fetch, begin=0x1000, end=0x2FFF)

    def fetch(self, cpu, pc, size, data):
        Startup.check_vector(self.vm, cpu, pc, size, data)

    def test_guest_patch_changes_operand_at_cached_address(self):
        self.cpu.mem_write(
            0x1000, bytes.fromhex("c5 f8 57 c0")
        )  # vxorps xmm0,xmm0,xmm0
        self.cpu.reg_write(UC_X86_REG_YMM0, (1 << 256) - 1)
        self.cpu.emu_start(0x1000, 0x1004)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM0), 0)
        # mov byte ptr [rip + d], 0xc1; jmp 0x1000. The replacement uses xmm1.
        self.cpu.mem_write(
            0x1100,
            b"\xc6\x05"
            + struct.pack("<i", 0x1003 - 0x1107)
            + b"\xc1\xe9"
            + struct.pack("<i", 0x1000 - 0x110C),
        )
        self.cpu.reg_write(UC_X86_REG_YMM0, (1 << 255) | 0x12)
        self.cpu.reg_write(UC_X86_REG_YMM1, 0xA0)
        self.cpu.emu_start(0x1100, 0x1004, count=8)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM0), 0xB2)

    def test_cached_code_cannot_run_after_execute_permission_removed(self):
        self.cpu.mem_write(0x1000, bytes.fromhex("c5 f8 57 c0"))
        self.cpu.emu_start(0x1000, 0x1004)
        self.cpu.mem_protect(0x1000, 4096, UC_PROT_READ)
        self.cpu.reg_write(UC_X86_REG_YMM0, 7)
        with self.assertRaises(UcError) as fault:
            self.cpu.emu_start(0x1000, 0x1004)
        self.assertEqual(fault.exception.errno, UC_ERR_FETCH_PROT)
        self.assertIsNone(self.vm.vector.execute(self.cpu, 0x1000))
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM0), 7)

    def test_invalid_size_sentinel_at_boundary_respects_both_pages(self):
        self.cpu.mem_map(0x2000, 4096, UC_PROT_READ)
        self.cpu.mem_write(0x1FFE, bytes.fromhex("c5 f8 57 c0"))
        self.cpu.reg_write(UC_X86_REG_YMM0, 7)
        self.fetch(self.cpu, 0x1FFE, 0xF1F1F1F1, None)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM0), 7)
        with self.assertRaises(UcError) as fault:
            self.cpu.emu_start(0x1FFE, 0x2002)
        self.assertEqual(fault.exception.errno, UC_ERR_FETCH_PROT)
        self.cpu.mem_protect(0x2000, 4096, UC_PROT_EXEC)
        self.fetch(self.cpu, 0x1FFE, 0xF1F1F1F1, None)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_YMM0), 0)
        self.assertEqual(self.cpu.reg_read(UC_X86_REG_RIP), 0x2002)
