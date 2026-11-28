"""Compare RDS5 block ranges with the original reader and samples on disk."""

import argparse
import hashlib
import json
import struct
import subprocess
import zlib
from collections import Counter
from itertools import pairwise
from pathlib import Path

from unicorn import UC_ARCH_X86, UC_MODE_64, UC_PROT_EXEC, UC_PROT_READ, Uc
from unicorn import x86_const as x86

if __package__:
    from .check_archive_readers import IMAGE_HASH, require
else:
    from check_archive_readers import IMAGE_HASH, require

LOOKUP_RVA = 0x55B23E0
TABLE = 0x100000
OBJECT = 0x300000
STACK = OBJECT + 0x1008
RETURN = OBJECT + 0x2000


def physical(word):
    return (word & 0x3FFFFFF) << 8


def logical(word):
    return ((word >> 30) & 0x3FFFFF) << 12


def read_table(stream, size):
    header = stream.read(16)
    require(len(header) == 16, "short RDS5 header")
    magic, count, packed = struct.unpack("<4sIQ", header)
    require(magic == b"RDS5" and 2 <= count <= (size - 16) // 8, "invalid RDS5 table")
    raw = stream.read(count * 8)
    require(len(raw) == count * 8, "short RDS5 index")
    words = [w for (w,) in struct.iter_unpack("<Q", raw)]
    require(
        physical(words[0]) >= 16 + len(raw) and logical(words[0]) == 0,
        "first block intersects the table or starts at a nonzero logical position",
    )
    require(
        physical(words[-1]) == size and logical(words[-1]) == packed & ((1 << 40) - 1),
        "sentinel disagrees with physical or logical length",
    )
    return raw, words


def block_sizes(first, after):
    span = physical(after) - physical(first)
    logical_size = logical(after) - logical(first)
    trim = first >> 52
    require(span > trim and logical_size > 0, "invalid stored or logical block size")
    return span, logical_size, trim


class GuestLookup:
    def __init__(self, executable, cli):
        plan = json.loads(
            subprocess.check_output([cli, "inspect", executable, "--json"])
        )
        data = executable.read_bytes()
        require(
            plan["sha256"] == IMAGE_HASH == hashlib.sha256(data).hexdigest(),
            "unknown executable revision for block lookup",
        )
        text = next(s for s in plan["sections"] if s["name"] == ".text")
        page = LOOKUP_RVA & -4096
        offset = text["offset"] + page - text["rva"]
        self.entry = plan["base"] + LOOKUP_RVA
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.mem_map(plan["base"] + page, 4096, UC_PROT_READ | UC_PROT_EXEC)
        self.cpu.mem_write(plan["base"] + page, data[offset : offset + 4096])
        self.cpu.mem_map(TABLE, OBJECT - TABLE)
        self.cpu.mem_map(OBJECT, 3 * 4096)

    def load(self, raw):
        require(len(raw) <= OBJECT - TABLE, "index exceeds probe scratch capacity")
        self.cpu.mem_write(TABLE, raw)
        self.cpu.mem_write(OBJECT + 0x20, struct.pack("<QI", TABLE, len(raw) // 8))

    def lookup(self, position, length):
        self.cpu.mem_write(STACK, struct.pack("<Q", RETURN))
        self.cpu.mem_write(STACK + 40, struct.pack("<QQ", OBJECT + 0x50, OBJECT + 0x58))
        for reg, value in (
            (x86.UC_X86_REG_RSP, STACK),
            (x86.UC_X86_REG_RDX, OBJECT),
            (x86.UC_X86_REG_R8, position),
            (x86.UC_X86_REG_R9, length),
        ):
            self.cpu.reg_write(reg, value)
        self.cpu.emu_start(self.entry, RETURN, count=2048)
        require(
            self.cpu.reg_read(x86.UC_X86_REG_RIP) == RETURN, "lookup did not return"
        )
        if not self.cpu.reg_read(x86.UC_X86_REG_RAX) & 0xFF:
            return None
        return struct.unpack("<QQ", self.cpu.mem_read(OBJECT + 0x50, 16))


def check_file(file, guest):
    result = Counter()
    with file.open("rb") as stream:
        raw, words = read_table(stream, file.stat().st_size)
        guest.load(raw)
        for first, after in pairwise(words):
            span, logical_size, trim = block_sizes(first, after)
            result["blocks"] += 1
            result["stored_bytes"] += span - trim
            result["trimmed_bytes"] += trim
            result["logical_bytes"] += logical_size
        stream.seek(physical(words[0]))
        if stream.read(4) == b"ETPF":
            result["first_block_etpf"] += 1
        # Keep the same positions even if a sampled block uses another format.
        for index in sorted({0, 1, len(words) // 2, len(words) - 2} - {len(words) - 1}):
            first, after = words[index : index + 2]
            span, logical_size, trim = block_sizes(first, after)
            stored = span - trim
            require(
                guest.lookup(logical(first), logical_size) == (physical(first), stored),
                "guest single-block range differs from the index",
            )
            result["guest_single_block_ranges"] += 1
            stream.seek(physical(first))
            if stream.read(2) != b"\x78\xda":
                continue
            require(
                max(stored, logical_size) <= 8 * 1024**2,
                "sample exceeds probe size bound",
            )
            stream.seek(physical(first))
            decoder = zlib.decompressobj()
            data = stream.read(stored)
            require(
                len(data) == stored
                and len(decoder.decompress(data, logical_size + 1)) == logical_size
                and decoder.eof
                and not decoder.unused_data
                and not decoder.unconsumed_tail,
                "zlib stream disagrees with stored or logical range",
            )
            result["zlib_sample_streams"] += 1
        if len(words) >= 3:
            start, second, end = words[:3]
            require(
                guest.lookup(logical(start), logical(second) - logical(start) + 1)
                == (physical(start), physical(end) - physical(start) - (second >> 52)),
                "guest multi-block range differs from the index",
            )
            result["guest_multi_block_ranges"] += 1
        require(
            guest.lookup(logical(words[-1]), 1) is None, "sentinel accepted as data"
        )
        result["guest_sentinel_rejections"] += 1
    return result


def run(executable, cli):
    guest = GuestLookup(executable, cli)
    files = sorted(executable.parent.rglob("*.ttpf"))
    require(bool(files), "no TTPF containers")
    counts = Counter(files=len(files))
    for file in files:
        counts.update(check_file(file, guest))
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--cli", type=Path, default=Path("target/release/velkin.exe"))
    args = parser.parse_args()
    print(json.dumps(run(args.executable.resolve(), args.cli.resolve()), indent=2))
