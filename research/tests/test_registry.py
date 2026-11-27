"""Read the profile as a guest would, including size-only and failed queries."""

import struct
import unittest

from unicorn import UC_ARCH_X86, UC_MODE_64, Uc
from unicorn.x86_const import (
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RSP,
)

from velkin_reference.memory import DWORD, QWORD
from velkin_reference.registry import MACHINE, OEM, Registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 4096)
        self.cpu.mem_write(0x1000, (OEM.upper() + "\0").encode("utf-16-le"))
        self.cpu.mem_write(0x1100, "ConsoleMode\0".encode("utf-16-le"))
        self.cpu.reg_write(UC_X86_REG_RSP, 0x1800)

    def open(self, profile):
        self.registry = Registry(profile)
        self.cpu.reg_write(UC_X86_REG_RCX, MACHINE)
        self.cpu.reg_write(UC_X86_REG_RDX, 0x1000)
        self.cpu.reg_write(UC_X86_REG_R8, 0x1200)
        self.assertEqual(self.registry.invoke(self.cpu, "RegOpenKeyW"), 0)
        handle = QWORD.unpack(self.cpu.mem_read(0x1200, 8))[0]
        self.cpu.reg_write(UC_X86_REG_RCX, handle)
        self.cpu.reg_write(UC_X86_REG_RDX, 0x1100)
        self.cpu.reg_write(UC_X86_REG_R8, 0)
        self.cpu.reg_write(UC_X86_REG_R9, 0x1208)

    def query(self, size, output=0x1300):
        self.cpu.mem_write(0x1828, struct.pack("<QQ", output, 0x1210))
        self.cpu.mem_write(0x1210, DWORD.pack(size))
        return self.registry.invoke(self.cpu, "RegQueryValueExW")

    def test_model_values_and_handle_lifetime(self):
        for profile, mode in (("series-x", 0x64), ("series-s", 0x60)):
            with self.subTest(profile=profile):
                self.open(profile)
                self.assertEqual(self.query(4), 0)
                self.assertEqual(self.cpu.mem_read(0x1300, 4), DWORD.pack(mode))
                self.assertEqual(self.cpu.mem_read(0x1208, 4), DWORD.pack(4))
                self.assertEqual(self.registry.invoke(self.cpu, "RegCloseKey"), 0)
                self.assertEqual(self.query(4), 6)

    def test_size_query_and_short_buffer(self):
        self.open("series-s")
        self.assertEqual(self.query(0, output=0), 0)
        self.assertEqual(self.cpu.mem_read(0x1210, 4), DWORD.pack(4))
        self.cpu.mem_write(0x1300, b"abcd")
        self.assertEqual(self.query(3), 234)
        self.assertEqual(bytes(self.cpu.mem_read(0x1300, 4)), b"abcd")
        self.assertEqual(self.cpu.mem_read(0x1210, 4), DWORD.pack(4))
