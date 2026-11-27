"""Build and test the portable x64 Windows distribution. Run with Python 3.11+."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "target"
PLATFORM = "x86_64-pc-windows-msvc"
PYTHON = "python-3.14.10-embed-amd64.zip"
CAPSTONE = "capstone-5.0.7-py3-none-win_amd64.whl"
UNICORN = "unicorn-2.1.4-cp37-abi3-win_amd64.whl"
UNICORN_SOURCE = "unicorn-2.1.4-upstream.tar.gz"
CAPSTONE_SOURCE = "capstone-5.0.7.tar.gz"
PYTHON_SBOM = PYTHON + ".spdx.json"

# SHA-256 from the release records of CPython and PyPI, or the exact
# upstream artifact identified by its Git commit or release documentation URL.
ARTIFACTS = {
    PYTHON: (
        f"https://www.python.org/ftp/python/3.14.10/{PYTHON}",
        "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159",
    ),
    PYTHON_SBOM: (
        f"https://www.python.org/ftp/python/3.14.10/{PYTHON_SBOM}",
        "d448c78f5fae170303d3b022d61507155e4d30e541c32bff3a4edd2b1e8dbf47",
    ),
    "python-license.html": (
        "https://docs.python.org/release/3.14.10/license.html",
        "a264986fb67db8f5e9eb890a1e82d7983ae1059ecd2fd5eb3209edee68849d78",
    ),
    CAPSTONE: (
        f"https://files.pythonhosted.org/packages/70/39/2138d890a8e827636b9de9924fbc8527fe83e38b6d26605b30ac55e30ebe/{CAPSTONE}",
        "4ab8bcb7da8f221ff45926ca168ca33e76f7237d06fbf3c10780002faa2670e1",
    ),
    CAPSTONE_SOURCE: (
        f"https://files.pythonhosted.org/packages/dd/9c/28b11f64e2425774efb21c206a6e952cfce6e3e2ef3e4b63cdae32ccd8a5/{CAPSTONE_SOURCE}",
        "796bdd69b05fa124fc2aa2e74b9a0b3d4c4e7f3e02add5e583cf2f3bca282ede",
    ),
    UNICORN: (
        f"https://files.pythonhosted.org/packages/70/3d/de7be9bd1addabe6d8a1369381f8a080400c349850e978689c5e18287957/{UNICORN}",
        "d7107500c64ce5c168fbff6bef9485b5db1350050036f4cea568650cf8bdbdf5",
    ),
    UNICORN_SOURCE: (
        "https://codeload.github.com/unicorn-engine/unicorn/tar.gz/8028ec436f2d9376525352dd38ed9ed6b9f6be10",
        "9d2a2003f0307d0fc239af439654564fe6ef17b67f7c5a39b45fad4c69d6aec2",
    ),
}


def fetch_inputs():
    cache = TARGET / "package-inputs"
    cache.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in ARTIFACTS.items():
        dest = cache / name
        if dest.is_file():
            with dest.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == expected:
                    continue
        request = urllib.request.Request(
            url, headers={"User-Agent": "Velkin packager"}
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"SHA-256 mismatch for {name}")
        dest.write_bytes(content)
    return cache


def cargo_metadata():
    return json.loads(
        subprocess.check_output(
            [
                "cargo",
                "+stable",
                "metadata",
                "--locked",
                "--format-version",
                "1",
                "--filter-platform",
                PLATFORM,
            ],
            cwd=ROOT,
        )
    )


def release_build():
    build = TARGET / "package-build"
    # Rust's file!() and panic locations otherwise record the builder's home.
    env = os.environ.copy()
    flags = [
        f"--remap-path-prefix={ROOT}=project",
        f"--remap-path-prefix={Path.home()}=builder",
        f"--remap-path-prefix={Path(os.getenv('CARGO_HOME', Path.home() / '.cargo'))}=cargo",
        "-Clink-arg=/PDBALTPATH:%_PDB%",
    ]
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    subprocess.run(
        [
            "cargo",
            "+stable",
            "build",
            "--release",
            "--locked",
            "--target",
            PLATFORM,
            "--target-dir",
            str(build),
        ],
        check=True,
        cwd=ROOT,
        env=env,
    )
    return build / PLATFORM / "release"


def put(archive, name, content):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"unsafe package entry {name}")
    if name in archive.NameToInfo:
        raise ValueError(f"duplicate package entry {name}")
    archive.writestr(zipfile.ZipInfo(name), content, compress_type=zipfile.ZIP_DEFLATED)


def notice(path):
    return path.name.upper().startswith(
        ("LICENSE", "LICENCE", "COPYING", "COPYRIGHT", "NOTICE", "UNLICENSE", "AUTHORS")
    )


def rust_notices(archive, metadata):
    for pkg in metadata["packages"]:
        if pkg["source"] is None:
            continue
        base = Path(pkg["manifest_path"]).parent
        names = [p for p in base.iterdir() if p.is_file() and notice(p)]
        for directory in ("licenses", "LICENSES"):
            folder = base / directory
            if folder.is_dir():
                names.extend(p for p in folder.rglob("*") if p.is_file())
                break
        if not names:
            raise ValueError(f"no packaged license file for {pkg['name']}")
        for p in sorted(set(names)):
            put(
                archive,
                f"notices/rust/{pkg['name']}-{pkg['version']}/{p.relative_to(base).as_posix()}",
                p.read_bytes(),
            )
    sysroot = subprocess.check_output(
        ["rustc", "+stable", "--print", "sysroot"], text=True
    ).strip()
    std_notice = Path(sysroot) / "share/doc/rust/COPYRIGHT-library.html"
    put(
        archive,
        "notices/rust/standard-library/COPYRIGHT-library.html",
        std_notice.read_bytes(),
    )


def package(archive, release, cache, metadata, native):
    mapping = {
        "velkin-launcher.exe": "Velkin.exe",
        "velkin-desktop.exe": "velkin-desktop.exe",
        "velkin.exe": "velkin-cli.exe",
        "velkin_simd.dll": "velkin_simd.dll",
    }
    for src, dest in mapping.items():
        data = (release / src).read_bytes()
        for directory in (ROOT, Path.home()):
            for encoding in ("utf-8", "utf-16-le"):
                if str(directory).lower().encode(encoding) in data.lower():
                    raise ValueError(f"local build path in {src}")
        put(archive, dest, data)
    receipt = (native / "build.json").read_bytes()
    recorded = json.loads(receipt)["sha256"]
    for source, dest in (
        ("velkin-engine.exe", "native/velkin-engine.exe"),
        ("LICENSE", "notices/native/LICENSE"),
        ("source.zip", "sources/native.zip"),
    ):
        data = (native / source).read_bytes()
        if hashlib.sha256(data).hexdigest() != recorded[source]:
            raise ValueError(f"native build receipt mismatch for {source}")
        put(archive, dest, data)
    put(archive, "native/build.json", receipt)
    put(archive, "LICENSE.txt", (ROOT / "LICENSE").read_bytes())
    put(archive, "RELEASE_NOTES.md", (ROOT / "RELEASE_NOTES.md").read_bytes())
    # The reference CPU module imports Unicorn (GPLv2 only): GPL-2.0-or-later.
    put(archive, "runtime/velkin_reference/LICENSE", (ROOT / "research/LICENSE").read_bytes())
    for module in sorted((ROOT / "research/velkin_reference").glob("*.py")):
        put(archive, "runtime/velkin_reference/" + module.name, module.read_bytes())
    with zipfile.ZipFile(cache / PYTHON) as python:
        for member in python.namelist():
            put(archive, "runtime/" + member, python.read(member))
        # The launcher and CLI import this CRT before a worker process exists.
        put(archive, "vcruntime140.dll", python.read("vcruntime140.dll"))
    for wheel in (CAPSTONE, UNICORN):
        with zipfile.ZipFile(cache / wheel) as deps:
            for member in deps.namelist():
                path = PurePosixPath(member)
                if path.suffix in (".py", ".dll") or notice(path):
                    put(archive, "runtime/" + member, deps.read(member))
    for name in (PYTHON_SBOM, "python-license.html"):
        put(archive, "notices/python/" + name, (cache / name).read_bytes())
    for name, component in ((CAPSTONE_SOURCE, "capstone"), (UNICORN_SOURCE, "unicorn")):
        with tarfile.open(cache / name) as source:
            for member in source:
                path = PurePosixPath(member.name)
                if member.isfile() and notice(path):
                    put(
                        archive,
                        f"notices/{component}/" + "/".join(path.parts[1:]),
                        source.extractfile(member).read(),
                    )
    put(archive, "sources/" + UNICORN_SOURCE, (cache / UNICORN_SOURCE).read_bytes())
    rust_notices(archive, metadata)
    put(archive, "notices/inputs.json", json.dumps(ARTIFACTS, indent=2).encode())
    put(
        archive,
        "notices/README.txt",
        (
            b"Velkin is free software under the GNU General Public License, version 3 or "
            b"later (LICENSE.txt); its reference CPU module (runtime/velkin_reference) is "
            b"under version 2 or later (runtime/velkin_reference/LICENSE).\n"
            b"Native play uses VIctory and victory-fork (both GPL-3.0-or-later) "
            b"with Velkin rendering code. "
            b"See notices/native/LICENSE, native/build.json and sources/native.zip.\n"
            b"The reference CPU uses CPython 3.14.10 (runtime/LICENSE.txt and notices/python), "
            b"Capstone 5.0.7 (notices/capstone), and Unicorn Engine 2.1.4 "
            b"(notices/unicorn). Unicorn's upstream engine is licensed under the GNU "
            b"General Public License version 2. The complete upstream source tree "
            b"for that release accompanies this package under sources/.\n"
            b"Notices for the Rust crates and standard library are under notices/rust. "
            b"Upstream download locations and SHA-256 digests are in inputs.json.\n"
        ),
    )


def verify(path):
    # Exercise the extracted archive from another working directory without an
    # installed Python, site-packages, or a development DLL on the search path.
    with tempfile.TemporaryDirectory(prefix="velkin-portable-") as tmp:
        copied = Path(tmp) / "application"
        with zipfile.ZipFile(path) as archive:
            archive.extractall(copied)
        env = {
            k: os.environ[k]
            for k in ("SystemRoot", "WINDIR", "TEMP", "TMP")
            if k in os.environ
        }
        env["PATH"] = str(Path(env["SystemRoot"]) / "System32")
        worker = [str(copied / "runtime/python.exe"), "-I", "-X", "utf8"]
        subprocess.run(
            [*worker, "-m", "velkin_reference", "--help"],
            check=True,
            cwd=tmp,
            env=env,
        )
        check = (
            "import pathlib, sys, capstone, unicorn, velkin_reference;"
            "base=pathlib.Path(sys.executable).parent;"
            "assert all(pathlib.Path(p).is_relative_to(base) for p in sys.path);"
            "assert all(pathlib.Path(m.__file__).is_relative_to(base) for m in "
            "(capstone, unicorn, velkin_reference));"
            "uc=unicorn.Uc(unicorn.UC_ARCH_X86,unicorn.UC_MODE_64);"
            "cs=capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_64);"
            "assert next(cs.disasm_lite(bytes([0xc3]),0))[2]=='ret';"
            "import ctypes; ctypes.CDLL(str(base.parent/'velkin_simd.dll'))"
        )
        subprocess.run([*worker, "-c", check], check=True, cwd=tmp, env=env)
        subprocess.run(
            [str(copied / "velkin-cli.exe"), "--version"],
            check=True,
            cwd=tmp,
            env=env,
        )
        subprocess.run(
            [str(copied / "Velkin.exe"), "--ui-demo", "--capture", str(Path(tmp) / "screens")],
            check=True, cwd=tmp, env=env,
        )
        subprocess.run(
            [str(copied / "native/velkin-engine.exe"), "run", "--help"],
            check=True,
            cwd=tmp,
            env=env,
            stdout=subprocess.DEVNULL,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if sys.platform != "win32":
        parser.error("the package build and portable smoke test require Windows")
    native = TARGET / "release/native"
    if not (native / "build.json").is_file():
        parser.error(
            "first run python scripts/build_native.py to build the native backend"
        )
    with (ROOT / "research/pyproject.toml").open("rb") as manifest:
        dependencies = tomllib.load(manifest)["project"]["dependencies"]
    if set(dependencies) != {"capstone==5.0.7", "unicorn==2.1.4"}:
        raise ValueError("update pinned packaging inputs for the worker dependencies")
    cache = fetch_inputs()
    metadata = cargo_metadata()
    release = release_build()
    version = next(
        p["version"]
        for p in metadata["packages"]
        if p["id"] == metadata["resolve"]["root"]
    )
    out = TARGET / "dist"
    out.mkdir(exist_ok=True)
    target = out / f"Velkin-{version}-windows-x64.zip"
    with tempfile.TemporaryDirectory(dir=out) as staging:
        candidate = Path(staging) / target.name
        with zipfile.ZipFile(candidate, "w") as archive:
            package(archive, release, cache, metadata, native)
        verify(candidate)
        os.replace(candidate, target)
    with target.open("rb") as stream:
        print(
            f"{target.name}  SHA-256 {hashlib.file_digest(stream, 'sha256').hexdigest()}"
        )


if __name__ == "__main__":
    main()
