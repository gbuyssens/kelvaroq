"""Verify the core model, capture replay, and guest Win32 buffer protocol."""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unicorn import UC_ARCH_X86, UC_MODE_64, UC_PROT_READ, Uc
from unicorn.x86_const import UC_X86_REG_RCX, UC_X86_REG_RDX

from velkin_reference.kernel import Kernel
from velkin_reference.memory import DWORD, GuestFault
from velkin_reference.topology import ProcessorInfo, manifest_smt_enabled


class ManifestTests(unittest.TestCase):
    def test_package_wide_setting_and_gdk_default(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "sample.exe"
            config = image.with_name("MicrosoftGame.Config")
            self.assertFalse(manifest_smt_enabled(image))
            for value, expected in (
                ("true", True),
                ("1", True),
                ("false", False),
                ("0", False),
            ):
                with self.subTest(value=value):
                    config.write_text(
                        '<Game xmlns="urn:test"><VirtualMachine>'
                        "<XboxAnacondaTitleMemory>Advanced</XboxAnacondaTitleMemory>"
                        f"<SMTEnabled> {value} </SMTEnabled>"
                        "<XboxLockhartTitleMemory>Advanced</XboxLockhartTitleMemory>"
                        "</VirtualMachine></Game>"
                    )
                    self.assertEqual(manifest_smt_enabled(image), expected)
            config.write_text(
                "<Game><Other><SMTEnabled>true</SMTEnabled></Other></Game>"
            )
            self.assertFalse(manifest_smt_enabled(image))

    def test_rejects_unreadable_or_ambiguous_manifest_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "sample.exe"
            config = image.with_name("MicrosoftGame.config")
            for xml in (
                "<Game",
                "<InvalidRoot/>",
                "<Game><VirtualMachine/><VirtualMachine/></Game>",
                "<Game><VirtualMachine><SMTEnabled>yes</SMTEnabled></VirtualMachine></Game>",
                "<Game><VirtualMachine><SMTEnabled>0</SMTEnabled>"
                "<SMTEnabled>1</SMTEnabled></VirtualMachine></Game>",
            ):
                with self.subTest(xml=xml):
                    config.write_text(xml)
                    with self.assertRaises(ValueError):
                        manifest_smt_enabled(image)


class ProcessorInfoTests(unittest.TestCase):
    def test_core_masks_and_smt_flag_match_the_32_byte_abi(self):
        for smt, masks in (
            (False, [1, 2, 4, 8, 16, 32, 64]),
            (True, [3, 12, 48, 192, 768, 3072, 12288]),
        ):
            with self.subTest(smt=smt):
                info = ProcessorInfo(smt_enabled=smt)
                self.assertEqual(info.source, "console-core-model")
                self.assertEqual(info.summary["cores"], 7)
                self.assertEqual(info.summary["logical_processors"], 14 if smt else 7)
                self.assertEqual(info.summary["record_count"], 7)
                self.assertEqual(
                    info.summary["core_masks"], [hex(mask) for mask in masks]
                )
                self.assertEqual(
                    info.records,
                    b"".join(
                        struct.pack("<QI4xB15x", mask, 0, int(smt)) for mask in masks
                    ),
                )

    def test_capture_priority_and_noncore_records_are_preserved(self):
        # Synthetic cache and core records include opaque bytes to check exact replay.
        capture = struct.pack("<QI20s", 3, 2, bytes(range(20))) + struct.pack(
            "<QI20x", 3, 0
        )
        with patch(
            "velkin_reference.topology.windows_records", return_value=capture
        ) as read:
            info = ProcessorInfo(capture, host_topology=True)
            self.assertEqual(info.source, "supplied-records")
            self.assertEqual(info.records, capture)
            self.assertEqual(info.summary["record_count"], 2)
            self.assertEqual(info.summary["core_masks"], ["0x3"])
            read.assert_not_called()
            self.assertEqual(ProcessorInfo(host_topology=True).source, "windows-host")
            read.assert_called_once_with()

    def test_capture_rejects_partial_empty_or_overlapping_core_records(self):
        for raw in (
            b"",
            bytes(31),
            bytes(32),
            struct.pack("<QI20x", 1, 2),
            struct.pack("<QI20x", 3, 0) + struct.pack("<QI20x", 2, 0),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ProcessorInfo(raw)

    def test_guest_size_query_retry_and_last_error(self):
        cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        cpu.mem_map(0x1000, 4096)
        cpu.mem_map(0x2000, 4096, UC_PROT_READ)
        kernel = Kernel(0, [], 0, {}, smt_enabled=True)
        api = "GetLogicalProcessorInformation"
        self.assertEqual(kernel.invoke(cpu, api), 0)
        self.assertEqual(kernel.invoke(cpu, "GetLastError"), 87)
        cpu.reg_write(UC_X86_REG_RDX, 0x1000)
        self.assertEqual(kernel.invoke(cpu, api), 0)
        self.assertEqual(kernel.invoke(cpu, "GetLastError"), 122)
        size = 7 * 32
        self.assertEqual(bytes(cpu.mem_read(0x1000, 4)), DWORD.pack(size))
        cpu.reg_write(UC_X86_REG_RCX, 0x1100)
        cpu.mem_write(0x1000, DWORD.pack(size - 1))
        self.assertEqual(kernel.invoke(cpu, api), 0)
        self.assertEqual(bytes(cpu.mem_read(0x1100, size)), bytes(size))
        self.assertEqual(bytes(cpu.mem_read(0x1000, 4)), DWORD.pack(size))
        self.assertEqual(kernel.invoke(cpu, api), 1)
        self.assertEqual(
            bytes(cpu.mem_read(0x1100, size)), kernel.processor_info.records
        )
        self.assertEqual(kernel.invoke(cpu, "GetLastError"), 122)
        cpu.reg_write(UC_X86_REG_RCX, 0x2000 - 32)
        with self.assertRaises(GuestFault):
            kernel.invoke(cpu, api)
        self.assertEqual(bytes(cpu.mem_read(0x2000 - 32, 32)), bytes(32))
