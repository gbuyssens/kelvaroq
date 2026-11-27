"""Build the pinned Windows x64 backend from its Git tree and ordered local patches."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "target/native"
PATCHES = ROOT / "native/patches"
RENDER_CRATE = ROOT / "native/velkin-render"
OUTPUT = ROOT / "target/release/native"
UPSTREAM = "https://github.com/fxceless/victory-fork.git"
REVISION = "f85101b6095e8cf3cecf8f0647cc7f5428daa177"
PLATFORM = "x86_64-pc-windows-msvc"
ENGINE = "velkin-engine.exe"
FILES = (ENGINE, "LICENSE", "source.zip", "build.json")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(checkout, *args):
    return subprocess.check_output(
        ["git", "-c", "core.autocrlf=false", "-c", "core.longpaths=true", *args],
        cwd=checkout,
        env=os.environ | {"TZ": "UTC"},
    )


def install_render_source(checkout, crate):
    dest = checkout / "crates/velkin-render"
    dest.mkdir(parents=True, exist_ok=False)
    src = crate / "src"
    if not src.is_dir() or src.is_symlink():
        raise ValueError("expected native/velkin-render/src")
    files = [crate / "Cargo.toml", *sorted(src.rglob("*"))]
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(crate.resolve()):
            raise ValueError(f"unexpected link in the render crate: {path.name}")
        if path.is_dir():
            continue
        copy = dest / path.relative_to(crate)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copy)
    git(checkout, "add", "--force", "--", "crates/velkin-render")
    # The public integration patch pins the first render crate. Keep the
    # staged source lock in step with the local crate included in this ZIP.
    lock = checkout / "Cargo.lock"
    if lock.is_file():
        version = tomllib.loads((dest / "Cargo.toml").read_text("utf8"))["package"][
            "version"
        ]
        data, count = re.subn(
            rb'(\[\[package\]\]\nname = "velkin-render"\nversion = ")[^"]+(")',
            lambda match: match[1] + version.encode("ascii") + match[2],
            lock.read_bytes(),
        )
        if count != 1:
            raise ValueError("expected one local render crate in the native lockfile")
        lock.write_bytes(data)
        git(checkout, "add", "--", "Cargo.lock")


def checkout_source(checkout, source, revision, patch_dir, render_crate):
    git(checkout, "init", "--quiet")
    git(checkout, "fetch", "--quiet", "--depth=1", "--no-tags", source, revision)
    git(checkout, "checkout", "--quiet", "--detach", revision)
    if git(checkout, "rev-parse", "HEAD").decode().strip() != revision:
        raise ValueError("unexpected source revision")
    patches = []
    ordered = sorted(
        [*patch_dir.glob("*.patch"), *patch_dir.glob("*.diff")],
        key=lambda path: path.name,
    )
    for patch in ordered:
        # Snapshot each input once; hash and apply the exact same bytes.
        data = patch.read_bytes()
        command = [
            "git",
            "-c",
            "core.autocrlf=false",
            "apply",
            "--index",
            "--binary",
            "-",
        ]
        try:
            subprocess.run(command, cwd=checkout, input=data, check=True)
        except subprocess.CalledProcessError as error:
            raise ValueError(
                f"failed to apply {patch.name} to the pinned source"
            ) from error
        patches.append({"name": patch.name, "sha256": hashlib.sha256(data).hexdigest()})
    install_render_source(checkout, render_crate)
    # Build the staged tree's normalized source even when the developer's
    # working copy of the overlay used different line endings.
    git(checkout, "checkout-index", "--all", "--force")
    tree = git(checkout, "write-tree").decode().strip()
    epoch = git(checkout, "show", "-s", "--format=%ct", revision).decode().strip()
    return tree, epoch, patches


@contextmanager
def build_drive(folder):
    # The upstream CLI uses env!("CARGO_MANIFEST_DIR") in compiled code. This
    # logical source path keeps the builder's personal directory out of the PE.
    for letter in "VXYZWUTSRQPONMLKJIHGFED":
        drive = f"{letter}:"
        path = Path(drive + "/")
        if (
            not path.exists()
            and subprocess.run(
                ["subst", drive, str(folder)], capture_output=True, check=False
            ).returncode
            == 0
        ):
            break
    else:
        raise OSError("no available drive letter for the isolated build")
    try:
        yield path
    finally:
        # If the mapping was externally redirected, leave that mapping alone.
        if path.exists() and path.samefile(folder):
            subprocess.run(["subst", drive, "/D"], check=True)


def archive_source(checkout, tree, epoch, dest):
    git(
        checkout,
        "archive",
        "--format=zip",
        "--prefix=velkin-engine/",
        f"--mtime=@{epoch}",
        f"--output={dest}",
        tree,
    )


def compile_backend(checkout, tree, epoch):
    channel = tomllib.loads((checkout / "rust-toolchain.toml").read_text("utf8"))[
        "toolchain"
    ]["channel"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", channel):
        raise ValueError("expected a pinned Rust release in rust-toolchain.toml")
    target = WORK / "build" / tree
    home = Path.home()
    flags = [
        f"--remap-path-prefix={checkout}=victory",
        f"--remap-path-prefix={ROOT}=velkin",
        f"--remap-path-prefix={home}=builder",
        f"--remap-path-prefix={os.environ.get('CARGO_HOME', home / '.cargo')}=cargo",
        "-Clink-arg=/PDBALTPATH:%_PDB%",
        "-Clink-arg=/Brepro",
    ]
    env = os.environ | {
        "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(flags),
        "SOURCE_DATE_EPOCH": epoch,
    }
    subprocess.run(
        [
            "cargo",
            f"+{channel}",
            "build",
            "--locked",
            "--release",
            "--target",
            PLATFORM,
            "--target-dir",
            target,
            "-p",
            "victory",
            "--bin",
            "victory",
        ],
        cwd=checkout,
        env=env,
        check=True,
    )
    binary = target / PLATFORM / "release/victory.exe"
    content = binary.read_bytes().lower()
    for path in (home, ROOT):
        for encoding in ("utf8", "utf-16-le"):
            if str(path).lower().encode(encoding) in content:
                raise ValueError("local directory embedded in the native executable")
    return binary, channel


def publish(staging, output):
    # Change only the four owned files. If Windows holds a file open, restore
    # the previous generation. The receipt is always installed last.
    import msvcrt

    output.mkdir(parents=True, exist_ok=True)
    lockpath = output.with_name(f".{output.name}.install.lock")
    with lockpath.open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        backups = staging / "previous"
        backups.mkdir()
        replaced = []
        try:
            for name in FILES:
                dest = output / name
                if dest.exists():
                    shutil.copy2(dest, backups / name)
                os.replace(staging / name, dest)
                replaced.append(name)
        except OSError:
            for name in reversed(replaced):
                backup = backups / name
                if backup.is_file():
                    os.replace(backup, output / name)
                else:
                    (output / name).unlink()
            raise


def run(source, output):
    if sys.platform != "win32":
        raise OSError(
            "the native release build requires Windows x64 and the MSVC linker"
        )
    if output == ROOT or (
        source != UPSTREAM and output.is_relative_to(Path(source).resolve())
    ):
        raise ValueError(
            "the output directory would overwrite the product or reference source tree"
        )
    WORK.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=WORK, prefix="checkout-") as temp,
        tempfile.TemporaryDirectory(
            dir=output.parent, prefix=".native-stage-"
        ) as stage,
    ):
        folder = Path(temp)
        checkout = folder / "source"
        checkout.mkdir()
        staging = Path(stage)
        tree, epoch, patches = checkout_source(
            checkout, source, REVISION, PATCHES, RENDER_CRATE
        )
        archive_source(checkout, tree, epoch, staging / "source.zip")
        with build_drive(folder) as drive:
            binary, channel = compile_backend(drive / "source", tree, epoch)
        shutil.copyfile(binary, staging / ENGINE)
        shutil.copyfile(checkout / "LICENSE", staging / "LICENSE")
        manifest = {
            "upstream": UPSTREAM,
            "revision": REVISION,
            "patched_tree": tree,
            "render_crate": {
                "path": "crates/velkin-render",
                "tree": git(checkout, "rev-parse", f"{tree}:crates/velkin-render")
                .decode()
                .strip(),
            },
            "patches": patches,
            "rust": channel,
            "platform": PLATFORM,
            "profile": "release",
            "cargo_lock_sha256": sha256(checkout / "Cargo.lock"),
            "sha256": {
                name: sha256(staging / name) for name in FILES if name != "build.json"
            },
        }
        (staging / "build.json").write_bytes(
            (json.dumps(manifest, indent=2) + "\n").encode()
        )
        publish(staging, output)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        help="use an existing local Git clone as the read-only source",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help="directory for the four release files",
    )
    args = parser.parse_args()
    result = run(
        str(args.source.resolve()) if args.source else UPSTREAM, args.output.resolve()
    )
    print(f"Installed {args.output} ({ENGINE} SHA-256 {result['sha256'][ENGINE]})")
