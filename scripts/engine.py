"""Keep native/patches as a git branch on top of the upstream engine.

native/patches is the published form of a branch: one commit per patch, on
top of the revision scripts/build_native.py pins. Work happens on the
branch, where git merges and remembers conflict resolutions (rerere); the
patches are exported from it.

    python scripts/engine.py checkout        # branch "engine" in target/engine
    python scripts/engine.py rebase <rev>    # move it onto a newer upstream
    python scripts/engine.py export          # write native/patches and the pin

After `rebase` stops on a conflict: resolve it in target/engine, run
`git rebase --continue` there, then `export`.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_native

PATCHES = build_native.PATCHES
RENDER_CRATE = build_native.RENDER_CRATE
BUILD_SCRIPT = ROOT / "scripts/build_native.py"
DEFAULT_DIR = ROOT / "target/engine"
BRANCH = "engine"
BASE_REF = "refs/velkin/base"
IDENTITY = ["-c", "user.name=Velkin engine", "-c", "user.email=engine@velkin.invalid"]


def git(checkout, *args, check=True, capture=True):
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.longpaths=true",
            *IDENTITY,
            *args,
        ],
        cwd=checkout,
        check=check,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.decode().strip() if capture else ""


def patch_files():
    return sorted(
        [*PATCHES.glob("*.patch"), *PATCHES.glob("*.diff")], key=lambda p: p.name
    )


def render_crate_name():
    return RENDER_CRATE.name


def install_render_crate(checkout):
    """The render crate lives in this repository; the patches name it as a
    sibling crate. Copied untracked so that the branch builds."""
    dest = checkout / "crates" / render_crate_name()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(RENDER_CRATE, dest, ignore=shutil.ignore_patterns("target"))
    exclude = checkout / ".git/info/exclude"
    line = f"crates/{render_crate_name()}/\n"
    if line not in exclude.read_text("utf8"):
        with exclude.open("a", encoding="utf8") as out:
            out.write(line)


def checkout(args):
    work = Path(args.dir)
    revision = args.revision or build_native.REVISION
    if work.exists():
        sys.exit(f"{work} exists; remove it or pass --dir")
    work.mkdir(parents=True)
    git(work, "init", "--quiet")
    git(work, "config", "rerere.enabled", "true")
    git(work, "config", "rerere.autoupdate", "true")
    git(
        work,
        "fetch",
        "--quiet",
        "--no-tags",
        build_native.UPSTREAM,
        revision,
        capture=False,
    )
    git(work, "checkout", "--quiet", "-B", BRANCH, revision)
    git(work, "update-ref", BASE_REF, revision)
    for patch in patch_files():
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.autocrlf=false",
                    "apply",
                    "--index",
                    "--binary",
                    str(patch),
                ],
                cwd=work,
                check=True,
            )
        except subprocess.CalledProcessError:
            sys.exit(f"{patch.name} does not apply on {revision[:12]}")
        git(work, "commit", "--quiet", "--allow-empty", "-m", patch.stem)
    if build_native.uses_render_crate(work):
        install_render_crate(work)
    print(f"{work}: branch {BRANCH} = {revision[:12]} + {len(patch_files())} patches")


def rebase(args):
    work = Path(args.dir)
    old = git(work, "rev-parse", BASE_REF)
    git(
        work,
        "fetch",
        "--quiet",
        "--no-tags",
        build_native.UPSTREAM,
        args.revision,
        capture=False,
    )
    new = git(work, "rev-parse", "FETCH_HEAD")
    git(work, "update-ref", "refs/velkin/next", new)
    result = subprocess.run(
        ["git", *IDENTITY, "rebase", "--onto", new, old, BRANCH], cwd=work, check=False
    )
    if result.returncode != 0:
        print(
            "\nConflicts: resolve them in",
            work,
            "then `git rebase --continue`;"
            " when the rebase is done run `python scripts/engine.py export --base next`.",
        )
        sys.exit(1)
    git(work, "update-ref", BASE_REF, new)
    print(f"{BRANCH} now on {new[:12]}; run `python scripts/engine.py export`")


def export(args):
    work = Path(args.dir)
    if args.base == "next":
        git(work, "update-ref", BASE_REF, git(work, "rev-parse", "refs/velkin/next"))
    base = git(work, "rev-parse", BASE_REF)
    commits = git(work, "rev-list", "--reverse", f"{base}..{BRANCH}").split()
    names = []
    for commit in commits:
        subject = git(work, "log", "-1", "--format=%s", commit)
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", subject).strip("-")
        if not re.match(r"^\d{4}-", name):
            sys.exit(f"commit {commit[:12]} ({subject!r}) must be named NNNN-what")
        names.append((commit, name + ".patch"))
    if len({n for _, n in names}) != len(names):
        sys.exit("two commits export to the same patch name")
    for old in patch_files():
        old.unlink()
    for commit, name in names:
        diff = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "show",
                "--format=",
                "--binary",
                "--full-index",
                commit,
            ],
            cwd=work,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        (PATCHES / name).write_bytes(diff)
    text = BUILD_SCRIPT.read_text("utf8")
    text, count = re.subn(
        r'^REVISION = "[0-9a-f]{40}"$', f'REVISION = "{base}"', text, flags=re.MULTILINE
    )
    if count != 1:
        sys.exit("expected one REVISION line in build_native.py")
    BUILD_SCRIPT.write_text(text, "utf8")
    print(f"{len(names)} patches written; pin {base[:12]}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("checkout")
    c.add_argument("--revision")
    c.set_defaults(run=checkout)
    r = sub.add_parser("rebase")
    r.add_argument("revision")
    r.set_defaults(run=rebase)
    e = sub.add_parser("export")
    e.add_argument("--base", choices=["current", "next"], default="current")
    e.set_defaults(run=export)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
