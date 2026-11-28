"""The CRT must propagate C errors and preserve caller state across constructors."""

import hashlib
import unittest
from unittest.mock import patch

from unicorn.x86_const import UC_X86_REG_RAX, UC_X86_REG_RSP, UC_X86_REG_YMM0

from velkin_reference.cpu import Startup
from velkin_reference.memory import QWORD


class InitializerTests(unittest.TestCase):
    def setUp(self):
        self.base = 0x140000000
        data = bytearray(0x2000)
        for offset, value in ((0x1000, 0), (0x1010, 7), (0x1020, 9)):
            data[offset : offset + 6] = b"\xb8" + value.to_bytes(4, "little") + b"\xc3"
        plan = {
            "base": self.base,
            "size": 0x2000,
            "headers": 0x200,
            "entry": 0x1000,
            "imports": [],
            "tls": None,
            "callbacks": [],
            "sha256": hashlib.sha256(data).hexdigest(),
            "sections": [
                {
                    "rva": 0x1000,
                    "virtual_size": 4096,
                    "raw_size": 4096,
                    "offset": 4096,
                    "flags": 0x60000000,
                }
            ],
        }
        self.vm = Startup(plan, bytes(data))
        self.sp = self.vm.stack_top - 0x28
        self.vm.cpu.reg_write(UC_X86_REG_RSP, self.sp)
        self.table = self.sp - 0x400
        self.end = self.table + 32
        targets = [0, self.base + 0x1000, self.base + 0x1010, self.base + 0x1020]
        self.vm.cpu.mem_write(self.table, b"".join(QWORD.pack(x) for x in targets))

    def test_c_initializer_stops_at_first_error(self):
        self.assertEqual(
            self.vm.run_initializers("_initterm_e", self.table, self.end), 7
        )
        self.assertEqual(self.vm.initializers, ["0x1000", "0x1010"])
        self.assertEqual(self.vm.cpu.reg_read(UC_X86_REG_RSP), self.sp)

    def test_cpp_initializer_completes_all_nonempty_slots(self):
        self.assertEqual(self.vm.run_initializers("_initterm", self.table, self.end), 0)
        self.assertEqual(self.vm.initializers, ["0x1000", "0x1010", "0x1020"])

    def test_misaligned_table_fails_before_first_call(self):
        self.vm.run_initializers("_initterm", self.table, self.end - 1)
        self.assertEqual(self.vm.blocker["reason"], "invalid CRT initializer table")
        self.assertFalse(self.vm.initializers)

    def test_guest_vex_xor_clears_stale_upper_lanes(self):
        address = self.base + 0x1060
        self.vm.cpu.mem_write(address, bytes.fromhex("c5 f8 57 c0 c3"))
        self.vm.cpu.reg_write(UC_X86_REG_YMM0, (1 << 256) - 1)
        self.assertTrue(self.vm.call(address, False), self.vm.blocker)
        self.assertEqual(self.vm.cpu.reg_read(UC_X86_REG_YMM0), 0)

    def test_guest_loop_resumes_across_slices(self):
        address = self.base + 0x1060
        # ecx = 12; eax = 0; do { ++eax; } while (--ecx); return
        self.vm.cpu.mem_write(
            address, bytes.fromhex("b9 0c 00 00 00 31 c0 ff c0 e2 fc c3")
        )
        with patch("velkin_reference.cpu.INSTRUCTIONS_PER_SLICE", 4):
            self.assertTrue(self.vm.call(address, False), self.vm.blocker)
        self.assertEqual(self.vm.cpu.reg_read(UC_X86_REG_RAX), 12)
