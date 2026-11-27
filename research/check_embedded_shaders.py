"""Report bounded DXBC containers and the bitcode field in embedded DXIL parts."""

import argparse
import hashlib
import json
import struct
import subprocess
from collections import Counter
from itertools import pairwise
from pathlib import Path

if __package__:
    from .check_archive_readers import IMAGE_HASH, require
else:
    from check_archive_readers import IMAGE_HASH, require


def dxbc_parts(data, start, limit):
    if start + 32 > limit or data[start : start + 4] != b"DXBC":
        return None
    version, size, count = struct.unpack_from("<III", data, start + 20)
    header_size = 32 + 4 * count
    if version != 1 or not count or not header_size <= size <= limit - start:
        return None
    parts = []
    for (offset,) in struct.iter_unpack("<I", data[start + 32 : start + header_size]):
        if offset < header_size or offset % 4 or offset + 8 > size:
            return None
        tag, payload_size = struct.unpack_from("<4sI", data, start + offset)
        end = offset + 8 + payload_size
        if end > size:
            return None
        parts.append((start + offset, start + end, tag))
    if any(a[1] > b[0] for a, b in pairwise(sorted(parts))):
        return None
    return parts


def inspect_program(data, start, end):
    """Offsets in DxilBitcodeHeader are relative to that header at start + 8."""
    if end - start < 24:
        return None
    version, words, magic, _, offset, length = struct.unpack_from(
        "<II4sIII", data, start
    )
    payload = start + 8 + offset
    if (
        magic != b"DXIL"
        or words * 4 != end - start
        or offset < 16
        or payload + length > end
    ):
        return None
    marker = data[payload : payload + min(4, length)]
    if marker == b"NOBC" and length == 4:
        field = "NOBC"
    elif marker == b"BC\xc0\xde":
        field = "LLVM signature"
    else:
        field = "other"
    return (version >> 16, (version >> 4) & 15, version & 15), field


def scan(data, sections):
    locations, tags, versions, fields = (Counter() for _ in range(4))
    for section in sections:
        position = section["offset"]
        end = position + section["raw_size"]
        require(0 <= position <= end <= len(data), "PE section out of bounds")
        while (start := data.find(b"DXBC", position, end)) != -1:
            position = start + 4
            parts = dxbc_parts(data, start, end)
            if parts is None:
                continue
            locations[section["name"]] += 1
            for part, limit, tag in parts:
                tags[tag.decode("ascii", errors="backslashreplace")] += 1
                if tag == b"DXIL":
                    program = inspect_program(data, part + 8, limit)
                    if program is None:
                        fields["invalid header"] += 1
                        continue
                    (stage, major, minor), field = program
                    versions[f"kind {stage}, model {major}.{minor}"] += 1
                    fields[field] += 1
    return {
        "containers_by_section": locations,
        "part_tags": tags,
        "program_versions": versions,
        "bitcode_fields": fields,
    }


def run(executable, cli):
    plan = json.loads(subprocess.check_output([cli, "inspect", executable, "--json"]))
    data = executable.read_bytes()
    require(
        plan["sha256"] == hashlib.sha256(data).hexdigest() == IMAGE_HASH,
        "unexpected executable revision for shader inventory",
    )
    return scan(data, plan["sections"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--cli", type=Path, default=Path("target/release/velkin.exe"))
    args = parser.parse_args()
    print(json.dumps(run(args.executable.resolve(), args.cli.resolve()), indent=2))
