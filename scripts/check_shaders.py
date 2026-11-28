"""Run pinned GFX10 reference programs on a named D3D12 hardware adapter."""

import argparse
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REFERENCE_URL = "https://github.com/fxceless/victory-fork.git"
RUNNER = "6f2d0571b1df59c97463e255c1a808befe2e408d"  # PR 212
SIGN_EXTENSION = "4cc0429f9029b8b348de7c258f9a87297c1d4538"  # PR 182
COMBINED_TREE = "d12d72e6a39c0313f4292b10f430ca2aac634cd8"
TERMINAL = "emit::valu_tests::terminal_rdna_compute"
TEXTURE_CASES = (
    "lanes_sample_the_texture_their_if_else_arm_chose",
    "lanes_sample_the_texture_their_arm_loaded_or_picked",
)


def run(*command, cwd=None, env=None, timeout=300):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() + "\n" + result.stdout[-4000:])
    return result.stdout


def extract_reference(cache, archive_dir, source):
    database = cache / "revisions.git"
    run("git", "init", "--bare", "--quiet", database)
    git = ["git", "--git-dir", str(database)]
    for name, commit in (("runner", RUNNER), ("sign-extension", SIGN_EXTENSION)):
        exists = subprocess.run(
            [*git, "cat-file", "-e", f"{commit}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if exists.returncode:
            run(*git, "fetch", "--no-tags", source, f"{commit}:refs/pins/{name}")
    tree = run(*git, "merge-tree", "--write-tree", RUNNER, SIGN_EXTENSION).strip()
    if tree != COMBINED_TREE:
        raise RuntimeError(f"unexpected reference source tree {tree}")
    archive = archive_dir / "reference.zip"
    run(*git, "archive", "--format=zip", f"--output={archive}", tree)
    checkout = archive_dir / "src"
    with zipfile.ZipFile(archive) as package:
        package.extractall(checkout)
    return checkout


def build(checkout, cache):
    output = run(
        "cargo",
        "+stable",
        "test",
        "--locked",
        "-p",
        "victory-shader",
        "--lib",
        "--no-run",
        "--message-format=json",
        cwd=checkout,
        env={**os.environ, "CARGO_TARGET_DIR": str(cache / "build")},
        timeout=600,
    )
    for line in output.splitlines():
        artifact = json.loads(line)
        if (
            artifact.get("reason") == "compiler-artifact"
            and artifact.get("executable")
            and artifact["target"]["name"] == "victory_shader"
            and artifact["profile"]["test"]
        ):
            return Path(artifact["executable"])
    raise RuntimeError("Cargo did not produce the shader test executable")


def shader_test(binary, name, environment, checkout):
    result = subprocess.run(
        [binary, "--exact", name, "--nocapture"],
        env=environment,
        cwd=checkout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    if (
        result.returncode
        or "1 passed;" not in result.stdout
        or "hardware device; skipped" in result.stdout
    ):
        raise RuntimeError(f"{name}\n{result.stdout}")
    return result.stdout


def check(binary, checkout, adapter):
    env = {**os.environ, "VICTORY_SHADER_ADAPTER": adapter, "VICTORY_WARP": "0"}
    # The terminal prints v0 for custom inputs. Assert it independently here.
    for width, opcode in ((8, "be861900"), (16, "be861a00")):
        for value in (0, (1 << (width - 1)) - 1, 1 << (width - 1), (1 << width) - 1):
            source = 0xFACE0000 | value
            expected = (
                value
                if value < 1 << (width - 1)
                else (value - (1 << width)) & 0xFFFFFFFF
            )
            output = shader_test(
                binary,
                TERMINAL,
                {
                    **env,
                    "RDNA_WORDS": opcode + " 7e000206",
                    "RDNA_INPUTS": f"{source:x} 0 0 0",
                },
                checkout,
            )
            actual = re.search(r"^v0 = 0x([0-9a-fA-F]{8})\b", output, re.MULTILINE)
            if not actual or int(actual[1], 16) != expected:
                raise RuntimeError(
                    f"{width}-bit sign extension of {source:#x}: expected {expected:#x}\n{output}"
                )
        print(f"{adapter}: four {width}-bit sign-extension results verified")
    for name in TEXTURE_CASES:
        shader_test(binary, "emit::waterfall_tests::" + name, env, checkout)
        print(f"{adapter}: 32 texture samples verified for {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adapter",
        required=True,
        help="case-insensitive part of the DXGI hardware name",
    )
    parser.add_argument(
        "--source",
        default=REFERENCE_URL,
        help="Git URL or local clone containing both pinned PRs",
    )
    args = parser.parse_args()
    cache = PROJECT / "target" / "shader-reference"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="velkin-shaders-") as directory:
        checkout = extract_reference(cache, Path(directory), args.source)
        binary = build(checkout, cache)
        check(binary, checkout, args.adapter)


if __name__ == "__main__":
    main()
