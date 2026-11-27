"""Exercise the semaphore ABI, error values and atomic count changes."""

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

from velkin_reference.kernel import Kernel
from velkin_reference.memory import GuestFault


class SemaphoreTests(unittest.TestCase):
    def setUp(self):
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(0x1000, 0x1000)
        self.cpu.reg_write(UC_X86_REG_RSP, 0x1000)
        self.kernel = Kernel(0x140000000, [], 0, {})

    def call(self, name, *args):
        for reg, value in zip(
            (UC_X86_REG_RCX, UC_X86_REG_RDX, UC_X86_REG_R8, UC_X86_REG_R9),
            (*args, 0, 0, 0, 0),
        ):
            self.cpu.reg_write(reg, value)
        return self.kernel.invoke(self.cpu, name)

    def create(self, initial=0, maximum=2, access=0x100002):
        self.cpu.mem_write(0x1028, struct.pack("<IIQ", 0, 0xFFFFFFFF, access))
        return self.call("CreateSemaphoreExW", 0, initial, maximum, 0)

    def test_counts_release_poll_and_close(self):
        handle = self.create()
        self.assertNotEqual(handle, 0)
        self.assertEqual(self.call("WaitForSingleObject", handle, 0), 258)
        self.cpu.mem_write(0x1100, bytes([255]) * 4)
        self.assertEqual(self.call("ReleaseSemaphore", handle, 2, 0x1100), 1)
        self.assertEqual(bytes(self.cpu.mem_read(0x1100, 4)), bytes(4))
        self.assertEqual(self.call("ReleaseSemaphore", handle, 1, 0), 0)
        self.assertEqual(self.kernel.last_error, 298)
        for expected in (0, 0, 258):
            self.assertEqual(self.call("WaitForSingleObjectEx", handle, 0, 0), expected)
        self.assertEqual(self.call("CloseHandle", handle), 1)
        self.assertEqual(self.call("WaitForSingleObject", handle, 0), 0xFFFFFFFF)
        self.assertEqual(self.kernel.last_error, 6)

    def test_invalid_initial_count_and_missing_access_rights(self):
        self.assertEqual(self.create(3, 2), 0)
        self.assertEqual(self.kernel.last_error, 87)
        handle = self.create(1, 1, 0x100000)
        self.assertEqual(self.call("ReleaseSemaphore", handle, 1, 0), 0)
        self.assertEqual(self.kernel.last_error, 5)
        self.assertEqual(self.call("WaitForSingleObject", handle, 0), 0)

    def test_blocking_wait_requires_a_guest_scheduler(self):
        handle = self.create()
        with self.assertRaisesRegex(GuestFault, "scheduling"):
            self.call("WaitForSingleObject", handle, 0xFFFFFFFF)

    def test_anonymous_events_reset_automatically_or_manually(self):
        for manual in (False, True):
            with self.subTest(manual=manual):
                handle = self.call("CreateEventA", 0, manual, 1, 0)
                self.assertEqual(self.call("WaitForSingleObject", handle, 0), 0)
                self.assertEqual(
                    self.call("WaitForSingleObject", handle, 0), 0 if manual else 258
                )
                self.assertEqual(self.call("ResetEvent", handle), 1)
                self.assertEqual(self.call("WaitForSingleObject", handle, 0), 258)
                for _ in range(2):
                    self.assertEqual(self.call("SetEvent", handle), 1)
                self.assertEqual(self.call("WaitForSingleObjectEx", handle, 0, 0), 0)
                self.assertEqual(
                    self.call("WaitForSingleObject", handle, 0), 0 if manual else 258
                )
                self.assertEqual(self.call("CloseHandle", handle), 1)
                self.assertEqual(self.call("SetEvent", handle), 0)
                self.assertEqual(self.kernel.last_error, 6)

    def test_cross_type_calls_preserve_both_objects(self):
        sem = self.create(1)
        event = self.call("CreateEventW", 0, 0, 0, 0)
        self.assertNotEqual(event, sem)
        self.assertEqual(self.call("SetEvent", sem), 0)
        self.assertEqual(self.call("ReleaseSemaphore", event, 1, 0), 0)
        self.assertEqual(self.call("WaitForSingleObject", sem, 0), 0)
        self.assertEqual(self.call("WaitForSingleObject", event, 0), 258)

    def test_mutex_ownership_recursion_and_close(self):
        handle = self.call("CreateMutexA", 0, 1, 0)
        self.assertEqual(self.call("WaitForSingleObject", handle, 0), 0)
        for _ in range(2):
            self.assertEqual(self.call("ReleaseMutex", handle), 1)
        self.assertEqual(self.call("ReleaseMutex", handle), 0)
        self.assertEqual(self.kernel.last_error, 288)
        self.assertEqual(self.call("WaitForSingleObject", handle, 0), 0)
        self.assertEqual(self.call("CloseHandle", handle), 1)
        self.assertEqual(self.call("ReleaseMutex", handle), 0)
        self.assertEqual(self.kernel.last_error, 6)
        unowned = self.call("CreateMutexW", 0, 0, 0)
        self.assertEqual(self.call("ReleaseMutex", unowned), 0)
        self.assertEqual(self.kernel.last_error, 288)
