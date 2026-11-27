"""Test embedded shaders against a pinned local reference checkout, via stdin."""

import argparse
import hashlib
import json
import os
import struct
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

if __package__:
    from .check_archive_readers import IMAGE_HASH, require
    from .check_embedded_shaders import dxbc_parts, inspect_program
else:
    from check_archive_readers import IMAGE_HASH, require
    from check_embedded_shaders import dxbc_parts, inspect_program

REVISION = "2119b0e44c53aa9648307b0c5c73590290305b1f"
ROOT = Path(__file__).resolve().parents[1]


def shader_inputs(data, sections):
    packet = bytearray()
    sources = []
    for section in sections:
        position = section["offset"]
        end = position + section["raw_size"]
        require(0 <= position <= end <= len(data), "invalid PE section range")
        while (start := data.find(b"DXBC", position, end)) >= 0:
            position = start + 4
            parts = dxbc_parts(data, start, end)
            if parts is None:
                continue
            (size,) = struct.unpack_from("<I", data, start + 24)
            rva = section["rva"] + start - section["offset"]
            tags = {tag: (first + 8, limit) for first, limit, tag in parts}
            program = inspect_program(data, *tags[b"DXIL"]) if b"DXIL" in tags else None
            sources.append(
                {
                    "rva": hex(rva),
                    "version": program[0] if program else None,
                    "bitcode": program[1] if program else None,
                    "private_bytes": tags[b"PRIV"][1] - tags[b"PRIV"][0]
                    if b"PRIV" in tags
                    else None,
                }
            )
            packet.extend(struct.pack("<II", rva, size))
            packet.extend(memoryview(data)[start : start + size])
    return sources, packet


def reference_build(reference, binary):
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=reference, text=True
    ).strip()
    require(revision == REVISION, "expected the pinned reference commit " + REVISION)
    changes = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--",
            "Cargo.toml",
            "Cargo.lock",
            ".cargo",
            "crates/victory-shader",
        ],
        cwd=reference,
    )
    require(
        not changes, "reference shader source or build inputs contain local changes"
    )
    target = ROOT / "target/shader-reference"
    output = subprocess.check_output(
        [
            "cargo",
            "+stable",
            "build",
            "--locked",
            "--manifest-path",
            reference / "Cargo.toml",
            "-p",
            "victory-shader",
            "--features",
            "host",
            "--target-dir",
            target,
            "--message-format=json",
        ],
        cwd=reference,
        text=True,
        encoding="utf8",
    )
    libraries = {}
    for line in output.splitlines():
        event = json.loads(line)
        if event["reason"] == "compiler-artifact":
            name = event["target"]["name"]
            for filename in event["filenames"]:
                if name in ("victory_shader", "windows") and filename.endswith(".rlib"):
                    libraries[name] = Path(filename)
    require(
        libraries.keys() == {"victory_shader", "windows"},
        "reference build produced no shader or D3D12 library",
    )
    subprocess.run(
        [
            "rustc",
            "+stable",
            "--edition",
            "2024",
            Path(__file__).with_suffix(".rs"),
            "-L",
            f"dependency={libraries['windows'].parent}",
            "--extern",
            f"windows={libraries['windows']}",
            "--extern",
            f"victory_shader={libraries['victory_shader']}",
            "-o",
            binary,
        ],
        check=True,
    )


def qualification(sources, rows, pipelines, draw):
    by_stage = Counter(
        row["detail"] for row in rows if row["step"] == "stage" and row["ok"]
    )
    addresses = Counter(source["rva"] for source in sources)
    outcomes = Counter((row["step"], row["ok"]) for row in rows)
    checks = {}
    for step in ("container", "native", "decoded", "translation"):
        finished = Counter(
            row["rva"] for row in rows if row["step"] == step and row["ok"]
        )
        checks[step] = finished == addresses and outcomes[step, False] == 0
    checks["native_stages"] = by_stage == {"Vs": 1, "Ps": 9}
    checks["stripped_bitcode"] = all(
        source["bitcode"] == "NOBC" for source in sources
    ) and (
        Counter(
            row["rva"]
            for row in rows
            if row["step"] == "dxil"
            and not row["ok"]
            and row["detail"] == "malformed shader: bitcode lacks LLVM magic"
        )
        == addresses
    )
    if pipelines or draw:
        adapters = outcomes["adapter", True]
        checks["hardware_adapters"] = adapters > 0 and outcomes["adapter", False] == 0
        checks["hardware_pipelines"] = (
            outcomes["pipeline", True] == 9 * adapters
            and not outcomes["pipeline", False]
        )
        if draw:
            checks["pixel_readback"] = (
                outcomes["pixels", True] == adapters and not outcomes["pixels", False]
            )
    return checks


def run(executable, cli, reference, pipelines, draw=False):
    plan = json.loads(subprocess.check_output([cli, "inspect", executable, "--json"]))
    data = executable.read_bytes()
    require(
        plan["sha256"] == hashlib.sha256(data).hexdigest() == IMAGE_HASH,
        "unexpected PE revision",
    )
    sources, packet = shader_inputs(data, plan["sections"])
    require(len(sources) == 10, "unexpected number of embedded containers")
    del data
    with tempfile.TemporaryDirectory(prefix="velkin-shader-reference-") as tmp:
        probe = Path(tmp) / "reference-probe.exe"
        reference_build(reference, probe)
        # Run the build's defaults regardless of a developer's diagnostic
        # switches. No shader bytes or disassembly are written by the probe.
        env = {
            k: v for k, v in os.environ.items() if not k.upper().startswith("VICTORY_")
        }
        command = [probe]
        if draw:
            command.append("--draw")
        elif pipelines:
            command.append("--pipelines")
        stdout = subprocess.check_output(command, input=packet, env=env, cwd=tmp)
    observations = []
    counts = Counter()
    for line in stdout.decode("utf8").splitlines():
        rva, step, status, detail = line.split("\t", maxsplit=3)
        require(status in ("true", "false"), "invalid probe result")
        observations.append(
            {"rva": rva, "step": step, "ok": status == "true", "detail": detail}
        )
        counts[f"{step}_{'ok' if status == 'true' else 'failed'}"] += 1
    require(
        counts["container_ok"] + counts["container_failed"] == len(sources),
        "missing reference results",
    )
    return {
        "reference": REVISION,
        "containers": sources,
        "counts": dict(counts),
        "checks": qualification(sources, observations, pipelines, draw),
        "observations": observations,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--cli", type=Path, default=ROOT / "target/release/velkin.exe")
    parser.add_argument(
        "--pipelines",
        action="store_true",
        help="Create candidate hardware PSOs without drawing",
    )
    parser.add_argument(
        "--draw",
        action="store_true",
        help="Create PSOs and check a 2x2 draw with synthetic vertices",
    )
    options = parser.parse_args()
    result = run(
        options.executable.resolve(),
        options.cli.resolve(),
        options.reference.resolve(),
        options.pipelines,
        options.draw,
    )
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if all(result["checks"].values()) else 1)
