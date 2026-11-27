"""End-to-end probes using PE bytes authored for this repository."""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from unicorn import UC_ARCH_X86, UC_MODE_64, UC_PROT_READ, UC_PROT_WRITE, Uc
from unicorn.x86_const import UC_X86_REG_R8, UC_X86_REG_RCX, UC_X86_REG_RDX

from velkin_reference.crt import compare_strings, copy_memory, fill_memory
from velkin_reference.memory import GuestFault

ROOT = Path(__file__).resolve().parents[2]
CLI = Path(os.environ.get("VELKIN_BIN", ROOT / "target" / "release" / "velkin.exe"))
BASE = 0x140000000


def fixture(with_import=False, executable=True, code=b"\xb8\x2a\0\0\0\xc3"):
    """One code section, optionally importing a deliberately unknown symbol."""
    image = bytearray(0x600)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 0x80)
    image[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 0x84, 0x8664, 1, 0, 0, 0, 0xF0, 0x22)
    opt = 0x98
    struct.pack_into("<H", image, opt, 0x20B)
    struct.pack_into("<II", image, opt + 16, 0x1000, 0x1000)
    struct.pack_into("<QII", image, opt + 24, BASE, 0x1000, 0x200)
    struct.pack_into("<II", image, opt + 56, 0x2000, 0x200)
    struct.pack_into("<H", image, opt + 68, 3)
    struct.pack_into("<I", image, opt + 108, 16)
    flags = 0x60000020 if executable else 0x40000040
    struct.pack_into(
        "<8sIIIIIIHHI",
        image,
        opt + 0xF0,
        b".text",
        0x400,
        0x1000,
        0x400,
        0x200,
        0,
        0,
        0,
        0,
        flags,
    )
    if with_import:
        # sub rsp, 40; call [rip + displacement]; add rsp, 40; ret
        code = (
            bytes.fromhex("48 83 ec 28 ff 15")
            + struct.pack("<i", 0x11A0 - 0x100A)
            + bytes.fromhex("48 83 c4 28 c3")
        )
        struct.pack_into("<II", image, opt + 120, 0x1100, 40)
        struct.pack_into("<IIIII", image, 0x300, 0x1190, 0, 0, 0x1160, 0x11A0)
        image[0x360:0x36A] = b"probe.dll\0"
        struct.pack_into("<Q", image, 0x390, 0x11B0)
        struct.pack_into("<Q", image, 0x3A0, 0x11B0)
        image[0x3B2:0x3BF] = b"UnknownProbe\0"
    image[0x200 : 0x200 + len(code)] = code
    return bytes(image)


class StartupTests(unittest.TestCase):
    def invoke(self, contents, command, *args):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.exe"
            path.write_bytes(contents)
            return subprocess.run(
                [str(CLI), command, str(path), *args],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf8",
                timeout=15,
            )

    def test_inspect_identifies_fixture(self):
        sample = fixture()
        result = self.invoke(sample, "inspect", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["sha256"], hashlib.sha256(sample).hexdigest())
        self.assertEqual((plan["base"], plan["entry"]), (BASE, 0x1000))

    def test_real_instructions_return_42(self):
        result = self.invoke(fixture(), "boot", "--python", sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["stage"], report["result"]), ("returned", 42))

    def test_unknown_import_identifies_guest_caller(self):
        result = self.invoke(
            fixture(with_import=True), "boot", "--python", sys.executable
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        stop = json.loads(result.stdout)["stop"]
        self.assertEqual(
            stop,
            {
                "reason": "unimplemented import",
                "symbol": "UnknownProbe",
                "dll": "probe.dll",
                "return_rva": "0x100a",
            },
        )

    def test_invalid_instruction_reports_the_mnemonic_and_guest_address(self):
        result = self.invoke(
            fixture(code=b"\x0f\x0b"), "boot", "--python", sys.executable
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        stop = json.loads(result.stdout)["stop"]
        self.assertEqual((stop["instruction"], stop["pc_rva"]), ("ud2", "0x1000"))

    def test_entry_requires_executable_section(self):
        result = self.invoke(fixture(executable=False), "inspect")
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside executable sections", result.stderr)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 4096, UC_PROT_READ | UC_PROT_WRITE)
        self.cpu.mem_map(0x2000, 4096, UC_PROT_READ)
        self.cpu.reg_write(UC_X86_REG_RCX, 0x1FFC)
        self.cpu.reg_write(UC_X86_REG_RDX, 0xABCD)

    def test_fill_returns_destination(self):
        self.cpu.reg_write(UC_X86_REG_R8, 4)
        self.assertEqual(fill_memory(self.cpu), 0x1FFC)
        self.assertEqual(bytes(self.cpu.mem_read(0x1FFC, 4)), bytes([0xCD]) * 4)

    def test_denied_span_preserves_first_page(self):
        self.cpu.reg_write(UC_X86_REG_R8, 5)
        with self.assertRaises(GuestFault):
            fill_memory(self.cpu)
        self.assertEqual(bytes(self.cpu.mem_read(0x1FFC, 4)), bytes(4))

    def test_string_comparison_preserves_case_and_unsigned_bytes(self):
        self.cpu.mem_write(0x1FFE, b"A\0")
        self.cpu.mem_write(0x2FFE, b"a\0")
        self.assertEqual(compare_strings(self.cpu, 0x1FFE, 0x2FFE), (-32) & 0xFFFFFFFF)
        self.assertEqual(
            compare_strings(self.cpu, 0x1FFE, 0x2FFE, 2, ignore_case=True), 0
        )
        self.cpu.mem_write(0x1FFE, b"\xff\0")
        self.assertEqual(compare_strings(self.cpu, 0x1FFE, 0x2FFE), 255 - 97)
        self.assertEqual(compare_strings(self.cpu, 0x2FFF, 0x1FFF), 0)

    def copy(self, destination, source, length):
        self.cpu.reg_write(UC_X86_REG_RCX, destination)
        self.cpu.reg_write(UC_X86_REG_RDX, source)
        self.cpu.reg_write(UC_X86_REG_R8, length)
        return copy_memory(self.cpu)

    def test_copy_returns_destination_and_accepts_empty_ranges(self):
        self.cpu.mem_write(0x2FF8, b"contents")
        self.assertEqual(self.copy(0x1FF8, 0x2FF8, 8), 0x1FF8)
        self.assertEqual(bytes(self.cpu.mem_read(0x1FF8, 8)), b"contents")
        self.assertEqual(self.copy(0x1FF8, 0x1FF8, 8), 0x1FF8)
        self.assertEqual(self.copy(2**64 - 1, 0, 0), 2**64 - 1)

    def test_invalid_source_or_destination_leaves_prefix_unchanged(self):
        self.cpu.mem_write(0x1FF8, b"original")
        self.cpu.mem_write(0x2FF8, b"replaced")
        for dst, src, count in (
            (0x1FF8, 0x2FFC, 8),
            (0x1FFC, 0x2FF8, 8),
            (0x1FF8, 2**64 - 4, 8),
        ):
            with self.subTest(dst=dst, src=src):
                with self.assertRaises(GuestFault):
                    self.copy(dst, src, count)
                self.assertEqual(bytes(self.cpu.mem_read(0x1FF8, 8)), b"original")

    def test_copy_overlap_in_both_directions_across_chunks(self):
        base = 0x4000
        self.cpu.mem_map(base, 0x12000, UC_PROT_READ | UC_PROT_WRITE)
        original = (bytes(range(251)) * 270)[:66015]
        count = len(original) - 13
        for dst, src in ((13, 0), (0, 13)):
            with self.subTest(dst=dst):
                self.cpu.mem_write(base, original)
                expected = bytearray(original)
                expected[dst : dst + count] = original[src : src + count]
                self.assertEqual(self.copy(base + dst, base + src, count), base + dst)
                self.assertEqual(self.cpu.mem_read(base, len(expected)), expected)


if __name__ == "__main__":
    unittest.main()
