"""Run the startup plan produced by the Rust PE inspector."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from unicorn import UcError
from unicorn.x86_const import UC_X86_REG_RAX

from .cpu import Startup
from .topology import manifest_smt_enabled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("console", choices=("series-x", "series-s"))
    parser.add_argument("legacy_capture", type=Path, nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--processor-info", type=Path)
    parser.add_argument("--host-topology", action="store_true")
    parser.add_argument("--package-root", type=Path)
    options = parser.parse_args()
    if options.legacy_capture is not None and options.processor_info is not None:
        parser.error("supply a single processor information capture")
    plan = json.load(sys.stdin)
    console = options.console
    capture_path = options.processor_info or options.legacy_capture
    capture = capture_path.read_bytes() if capture_path is not None else None
    smt = (
        manifest_smt_enabled(options.image, options.package_root)
        if capture is None and not options.host_topology
        else False
    )
    vm = Startup(
        plan,
        options.image.read_bytes(),
        console,
        capture,
        smt_enabled=smt,
        host_topology=options.host_topology,
    )
    completed = []
    if os.environ.get("VELKIN_LIVE_LOG") == "1":
        started = time.monotonic()

        def log(message):
            print(
                f"[{time.monotonic() - started:7.3f} s] {message}",
                file=sys.stderr,
                flush=True,
            )

        vm.log = log
        log(
            f"Profil {console} | {len(plan['imports'])} imports | CPU de référence prêt"
        )
    stages = [("tls-attach", a) for a in plan["callbacks"]]
    stages.append(("entry", plan["base"] + plan["entry"]))
    for stage, address in stages:
        if vm.log:
            vm.log(f"Entrée {stage}  RVA {address - plan['base']:#x}")
        if not vm.call(address, stage == "tls-attach"):
            if vm.log:
                vm.log(
                    "Arrêt  " + " | ".join(str(value) for value in vm.blocker.values())
                )
            print(
                json.dumps(
                    {
                        "stage": stage,
                        "console": console,
                        "processor_info": vm.kernel.processor_info.summary,
                        "completed": completed,
                        "stop": vm.blocker,
                        "calls": vm.calls,
                        "virtual_reservations": [
                            vars(r) for r in vm.graphics_memory.reservations
                        ],
                        "initializers_entered": len(vm.initializers),
                        "last_initializer": vm.initializers[-1:],
                    }
                )
            )
            return 2
        completed.append({"stage": stage, "rva": hex(address - plan["base"])})
        if vm.log:
            vm.log(f"Retour {stage}  RVA {address - plan['base']:#x}")
    print(
        json.dumps(
            {
                "stage": "returned",
                "console": console,
                "processor_info": vm.kernel.processor_info.summary,
                "completed": completed,
                "result": vm.cpu.reg_read(UC_X86_REG_RAX),
                "calls": vm.calls,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, UcError, RecursionError) as error:
        print(f"reference CPU: {error}", file=sys.stderr)
        sys.exit(1)
