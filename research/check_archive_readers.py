"""Check selected original x64 archive-reader blocks against local file headers."""

import argparse
import hashlib
import json
import struct
import subprocess
from pathlib import Path

from unicorn import UC_ARCH_X86, UC_MODE_64, UC_PROT_EXEC, UC_PROT_READ, Uc
from unicorn import x86_const as x86

IMAGE_HASH = "e95020b2fa4a721db3df59fdecf75621730ac559d8835252f6c3b658d5a08993"
HEADER = 0x100000
OBJECT = 0x200000
STACK = OBJECT + 0x1000
HEADER_CAPACITY = OBJECT - HEADER
PAGE = 4096


def require(condition, description):
    if not condition:
        raise ValueError(description)


class ReaderBlocks:
    """Only the original code pages under study and isolated scratch memory are mapped."""

    def __init__(self, executable, cli):
        plan = json.loads(
            subprocess.check_output([cli, "inspect", executable, "--json"])
        )
        self.base = plan["base"]
        section = next(item for item in plan["sections"] if item["name"] == ".text")
        data = executable.read_bytes()
        require(
            plan["sha256"] == hashlib.sha256(data).hexdigest() == IMAGE_HASH,
            "unknown executable revision for these RVAs",
        )
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        for routine in (0xF435F0, 0x1FB2600):
            page = routine & -PAGE
            offset = section["offset"] + page - section["rva"]
            self.cpu.mem_map(self.base + page, PAGE, UC_PROT_READ | UC_PROT_EXEC)
            self.cpu.mem_write(self.base + page, data[offset : offset + PAGE])
        self.cpu.mem_map(HEADER, HEADER_CAPACITY + 2 * PAGE)
        self.cpu.reg_write(x86.UC_X86_REG_RSP, STACK)

    def block(self, entry, *ends):
        pc = self.base + entry
        for _ in range(24):
            self.cpu.emu_start(pc, 0, count=1)
            pc = self.cpu.reg_read(x86.UC_X86_REG_RIP)
            if pc - self.base in ends:
                return pc - self.base
        raise ValueError(f"unexpected control flow from RVA {entry:#x}")

    def rds_recognized(self, prefix, size_read):
        self.cpu.mem_write(HEADER, prefix)
        self.cpu.mem_write(STACK + 0x3C, struct.pack("<I", size_read))
        self.cpu.reg_write(x86.UC_X86_REG_RAX, 1)  # preceding ReadFile succeeded
        self.cpu.reg_write(x86.UC_X86_REG_RBX, HEADER)
        return self.block(0xF43690, 0xF436A3, 0xF436EE) == 0xF436EE

    def rds_index(self, file):
        with file.open("rb") as stream:
            header = stream.read(16)
            require(
                len(header) == 16 and self.rds_recognized(header, 16),
                "RDS5 header rejected",
            )
            _, count, packed = struct.unpack("<4sIQ", header)
            self.cpu.reg_write(x86.UC_X86_REG_RSI, OBJECT)
            self.block(0xF436EE, 0xF4370C)
            table_bytes = self.cpu.reg_read(x86.UC_X86_REG_RCX)
            scalar = struct.unpack("<Q", self.cpu.mem_read(OBJECT + 8, 8))[0]
            require(
                table_bytes == 8 * count and scalar == packed & ((1 << 40) - 1),
                "table size or 40-bit field disagrees with the guest",
            )
            self.block(0xF43711, 0xF4372C)
            span = self.cpu.reg_read(x86.UC_X86_REG_R13)
            require(
                span == (table_bytes + 32 + PAGE - 1) & -PAGE, "bad aligned header span"
            )
            require(
                span <= min(HEADER_CAPACITY, file.stat().st_size),
                "header span outside file or probe",
            )
            payload = stream.read(span - len(header))
            require(len(payload) == span - 16, "short table read")
        self.cpu.mem_write(HEADER + 16, payload)
        self.cpu.reg_write(x86.UC_X86_REG_R12, HEADER)
        self.cpu.reg_write(x86.UC_X86_REG_R15, OBJECT)
        self.block(0xF4377E, 0xF4378B)
        require(
            self.cpu.reg_read(x86.UC_X86_REG_RDX) == HEADER + 16
            and self.cpu.reg_read(x86.UC_X86_REG_R8) == table_bytes,
            "unexpected memcpy arguments",
        )
        self.block(0xF43790, 0xF4379F)
        require(
            bytes(self.cpu.mem_read(OBJECT + 0x38, 16))
            == payload[table_bytes : table_bytes + 16],
            "trailer disagrees with guest",
        )
        return count

    def rpf_extent(self, header, mode):
        self.cpu.mem_write(STACK + 0x30, header)
        self.cpu.reg_write(x86.UC_X86_REG_RBP, mode)
        if self.block(0x1FB268B, 0x1FB2751, 0x1FB26AF) == 0x1FB2751:
            return None
        return self.cpu.reg_read(x86.UC_X86_REG_RCX)


def run(executable, cli):
    blocks = ReaderBlocks(executable, cli)
    require(
        not blocks.rds_recognized(b"BAD!" + bytes(12), 16),
        "RDS5 negative control failed",
    )
    require(
        not blocks.rds_recognized(b"RDS5" + bytes(12), 15), "RDS5 short header accepted"
    )
    require(blocks.rpf_extent(bytes(16), 0) is None, "RPF9 negative control failed")
    directory = executable.parent
    rpf = sorted(directory.rglob("*.rpf"))
    rds = sorted(directory.rglob("*.ttpf"))
    require(rpf and rds, "both archive families must be present beside the executable")
    for file in rpf:
        with file.open("rb") as stream:
            header = stream.read(16)
        magic, full, partial, _ = struct.unpack("<4sIII", header)
        require(
            magic == b"9FPR" and 16 <= partial <= full <= file.stat().st_size,
            "invalid RPF9 bounds",
        )
        for mode, expected in enumerate((full, partial)):
            require(
                blocks.rpf_extent(header, mode) == expected,
                "unexpected RPF9 prefix size",
            )
    entries = sum(blocks.rds_index(file) for file in rds)
    return {
        "rpf_headers": len(rpf),
        "rpf_size_selections": 2 * len(rpf),
        "rds_headers": len(rds),
        "rds_table_entries": entries,
        "negative_controls": 3,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--cli", type=Path, default=Path("target/release/velkin.exe"))
    args = parser.parse_args()
    print(json.dumps(run(args.executable.resolve(), args.cli.resolve()), indent=2))
