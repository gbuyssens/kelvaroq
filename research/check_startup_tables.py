"""Verify two index pools as the reference CPU executes normal TLS and startup."""

import argparse
import hashlib
import json
import os
import struct
import subprocess
from pathlib import Path

from unicorn import UC_HOOK_CODE
from unicorn.x86_const import UC_X86_REG_RAX, UC_X86_REG_RCX

from velkin_reference.cpu import Startup
from velkin_reference.memory import read_guest
from velkin_reference.topology import manifest_smt_enabled

if __package__:
    from .check_archive_readers import IMAGE_HASH, require
else:
    from check_archive_readers import IMAGE_HASH, require


class IndexPool:
    def __init__(self, vm, object_rva, offset, count, stride, checkpoints):
        self.base = vm.plan["base"]
        self.object_rva = object_rva
        self.offset = offset
        self.count = count
        self.record_bytes = count * stride
        self.expected = struct.pack(f"<{count}H", *range(1, count + 1))
        self.instances = []
        self.object = None
        for rva, callback in zip(
            checkpoints, (self.enter, self.latch, self.leave), strict=True
        ):
            address = self.base + rva
            vm.cpu.hook_add(UC_HOOK_CODE, callback, begin=address, end=address)

    def neighbours(self, cpu):
        table = self.object + self.offset
        # The word after the links is the mutable free-list head. The array of
        # records follows that head and was initialized before the SIMD loop.
        return (
            read_guest(cpu, table - 2, 2),
            read_guest(cpu, table + len(self.expected) + 2, self.record_bytes),
        )

    def enter(self, cpu, address, size, user_data):
        require(self.object is None, "overlapping index pool initialization")
        self.object = cpu.reg_read(UC_X86_REG_RAX)
        self.guards = self.neighbours(cpu)
        self.iterations = 0

    def latch(self, cpu, address, size, user_data):
        require(self.object is not None, "loop entered without initialization")
        require(
            cpu.reg_read(UC_X86_REG_RCX) == 16 * self.iterations,
            "unexpected loop index",
        )
        self.iterations += 1

    def leave(self, cpu, address, size, user_data):
        require(
            self.object == cpu.reg_read(UC_X86_REG_RAX),
            "unexpected pool base at return",
        )
        require(
            self.iterations == (self.count + 15) // 16, "incorrect SIMD iteration count"
        )
        require(
            cpu.reg_read(UC_X86_REG_RCX) == 16 * self.iterations,
            "incorrect final loop index",
        )
        table = self.object + self.offset
        links = read_guest(cpu, table, len(self.expected))
        require(
            links == self.expected,
            f"incorrect next-free links at RVA {table - self.base:#x}",
        )
        require(
            read_guest(cpu, table + len(links), 2) == bytes(2),
            "incorrect free-list head",
        )
        require(
            self.neighbours(cpu) == self.guards,
            "index initialization modified neighbouring data",
        )
        self.instances.append(
            {
                "object_rva": hex(self.object - self.base),
                "table_rva": hex(table - self.base),
                "entries": self.count,
                "iterations": self.iterations,
                "sha256": hashlib.sha256(links).hexdigest(),
            }
        )
        self.object = None


def run(executable, cli, console):
    plan = json.loads(subprocess.check_output([cli, "inspect", executable, "--json"]))
    require(
        plan["sha256"] == IMAGE_HASH,
        "unknown executable revision for startup table checks",
    )
    os.environ["VELKIN_SIMD_LIB"] = str(cli.with_name("velkin_simd.dll"))
    vm = Startup(
        plan,
        executable.read_bytes(),
        console,
        smt_enabled=manifest_smt_enabled(executable),
    )
    # Observe scalar instructions, including each RET after all vector stores.
    pools = (
        IndexPool(vm, 0x75A58C0, 0xA9C, 270, 24, (0x2471644, 0x24719D3, 0x2471A3F)),
        IndexPool(vm, 0x75A7ED0, 0x1410, 512, 68, (0x2471B21, 0x2471B3B, 0x2471B4B)),
    )
    stages = [(callback, True) for callback in plan["callbacks"]]
    stages.append((plan["base"] + plan["entry"], False))
    for address, tls in stages:
        if not vm.call(address, tls):
            break
    for pool in pools:
        require(
            pool.object is None, f"unfinished {pool.count}-entry pool at {vm.blocker}"
        )
        require(
            any(row["object_rva"] == hex(pool.object_rva) for row in pool.instances),
            f"expected {pool.count}-entry pool was not observed at {vm.blocker}",
        )
    return {
        "pools": [row for pool in pools for row in pool.instances],
        "startup_stop": vm.blocker,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--cli", type=Path, default=Path("target/release/velkin.exe"))
    parser.add_argument(
        "--console", choices=("series-s", "series-x"), default="series-x"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.executable.resolve(), args.cli.resolve(), args.console), indent=2
        )
    )
