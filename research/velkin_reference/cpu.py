"""Execute guest callbacks and dispatch imports at the x64 call boundary."""

import time

from unicorn import (
    UC_ARCH_X86,
    UC_ERR_INSN_INVALID,
    UC_HOOK_CODE,
    UC_MODE_64,
    Uc,
    UcError,
)
from unicorn.x86_const import (
    UC_X86_REG_MXCSR,
    UC_X86_REG_R8,
    UC_X86_REG_R9,
    UC_X86_REG_RAX,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RIP,
    UC_X86_REG_RSP,
)

from .crt import Crt, copy_memory, fill_memory
from .graphics_memory import GraphicsMemory
from .kernel import Kernel
from .memory import PAGE, QWORD, GuestFault, map_image, prepare_services, prepare_thread
from .registry import Registry
from .vector import VectorMoves

INSTRUCTIONS_PER_SLICE = 1_000_000


class Startup:
    def __init__(
        self,
        plan,
        data,
        console="series-x",
        processor_info=None,
        *,
        smt_enabled=False,
        host_topology=False,
    ):
        self.plan = plan
        self.cpu = Uc(UC_ARCH_X86, UC_MODE_64)
        self.cpu.reg_write(UC_X86_REG_MXCSR, 0x1F80)
        self.vector = VectorMoves()
        map_image(self.cpu, plan, data)
        self.cpu.hook_add(
            UC_HOOK_CODE,
            self.check_vector,
            begin=plan["base"],
            end=plan["base"] + plan["size"] - 1,
        )
        self.symbols, self.services, self.return_pc, thread = prepare_services(
            self.cpu, plan
        )
        self.stack_top, crt = prepare_thread(self.cpu, plan, thread)
        self.crt = Crt(self.cpu, crt)
        self.kernel = Kernel(
            plan["base"],
            self.symbols,
            self.services,
            self.crt.command_lines,
            processor_info,
            smt_enabled=smt_enabled,
            host_topology=host_topology,
        )
        self.registry = Registry(console)
        self.graphics_memory = GraphicsMemory()
        self.cpu.hook_add(
            UC_HOOK_CODE, self.service, begin=self.services, end=self.return_pc - 1
        )
        self.blocker = None
        self.calls = {}
        self.log = None
        self.initializers = []
        self.pending_init = None
        self.deadline = time.monotonic() + 30

    def check_vector(self, cpu, address, instruction_size, _):
        # Unicorn may accept VEX.128 while retaining stale high lanes.
        # Handle our verified instruction families before the native translator.
        try:
            # This callback follows an executable fetch. Read ahead only within
            # its page. Unicorn can report a sentinel size for unsupported VEX.
            if cpu.mem_read(address, 1)[0] not in (0xC4, 0xC5):
                return
            if address % PAGE <= PAGE - 15:
                code = bytes(cpu.mem_read(address, 15))
                following = self.vector.execute_fetched(cpu, address, code)
            else:
                following = self.vector.execute(cpu, address)
        except (UcError, GuestFault) as error:
            self.blocker = {"reason": str(error)}
            cpu.emu_stop()
            return
        if following is not None:
            cpu.reg_write(UC_X86_REG_RIP, following)

    def service(self, cpu, address, instruction_size, _):
        index, skew = divmod(address - self.services, 16)
        if skew or index >= len(self.symbols):
            self.blocker = {"reason": "invalid service slot"}
            cpu.emu_stop()
            return
        symbol = self.symbols[index]
        name, dll = symbol["name"], symbol["dll"]
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.log and self.calls[name] == 1:
            self.log(f"Service  {dll}!{name}")
        if dll.startswith("api-ms-win-crt-runtime-") and name in (
            "_initterm_e",
            "_initterm",
        ):
            self.pending_init = (
                name,
                cpu.reg_read(UC_X86_REG_RCX),
                cpu.reg_read(UC_X86_REG_RDX),
            )
            cpu.emu_stop()
            return
        failure = "unimplemented import"
        try:
            if dll.startswith("api-ms-win-core-") or dll in (
                "kernel32.dll",
                "kernelbase.dll",
            ):
                result = self.kernel.invoke(cpu, name)
            elif dll.startswith("api-ms-win-crt-"):
                result = self.crt.invoke(cpu, name)
            elif dll == "advapi32.dll":
                result = self.registry.invoke(cpu, name)
            elif dll == "d3d12_xs.dll" and name == "D3D12ConfigureVirtualMemoryX":
                result = self.graphics_memory.configure(cpu)
            elif dll == "xmem.dll" and name == "XMemVirtualAlloc":
                result = self.graphics_memory.allocate(cpu)
            elif dll == "vcruntime140.dll" and name == "memset":
                result = fill_memory(cpu)
            elif dll == "vcruntime140.dll" and name == "memcpy":
                result = copy_memory(cpu)
            else:
                result = NotImplemented
            if result is not NotImplemented:
                cpu.reg_write(UC_X86_REG_RAX, result)
                return
        except (GuestFault, UcError) as error:
            failure = str(error)
        (ret,) = QWORD.unpack(cpu.mem_read(cpu.reg_read(UC_X86_REG_RSP), 8))
        self.blocker = {
            "reason": failure,
            "dll": dll,
            "symbol": name,
            "return_rva": hex(ret - self.plan["base"]),
        }
        cpu.emu_stop()

    def call(self, address, tls):
        # Windows x64 entry has a return address and 32 bytes of shadow space.
        rsp = self.stack_top - 0x28
        self.cpu.mem_write(rsp, QWORD.pack(self.return_pc) + bytes(32))
        self.cpu.reg_write(UC_X86_REG_RSP, rsp)
        self.cpu.reg_write(UC_X86_REG_RCX, self.plan["base"] if tls else 0)
        self.cpu.reg_write(UC_X86_REG_RDX, 1 if tls else 0)
        self.cpu.reg_write(UC_X86_REG_R8, 0)
        self.cpu.reg_write(UC_X86_REG_R9, 0)
        return self.run_until_return(address)

    def run_until_return(self, address):
        try:
            while True:
                remaining = int((self.deadline - time.monotonic()) * 1_000_000)
                if remaining <= 0:
                    self.blocker = {"reason": "startup time limit"}
                    break
                try:
                    self.cpu.emu_start(
                        address,
                        self.return_pc,
                        timeout=min(remaining, 3_000_000),
                        count=INSTRUCTIONS_PER_SLICE,
                    )
                except UcError as error:
                    if error.errno != UC_ERR_INSN_INVALID:
                        raise
                    pc = self.cpu.reg_read(UC_X86_REG_RIP)
                    following = self.vector.execute(self.cpu, pc)
                    if following is None:
                        self.blocker = {
                            "reason": "guest CPU stopped",
                            "detail": str(error),
                        }
                        instruction = self.vector.decode(
                            pc, self.vector.fetch(self.cpu, pc)
                        )
                        if instruction is not None:
                            self.blocker["instruction"] = (
                                f"{instruction.mnemonic} {instruction.op_str}".strip()
                            )
                        break
                    address = following
                    continue
                if self.blocker:
                    break
                if not self.pending_init:
                    address = self.cpu.reg_read(UC_X86_REG_RIP)
                    if address == self.return_pc:
                        break
                    # Either the time slice or instruction slice expired. Resume
                    # from the live PC under the original process-wide deadline.
                    continue
                name, first, end = self.pending_init
                self.pending_init = None
                result = self.run_initializers(name, first, end)
                if self.blocker:
                    return False
                rsp = self.cpu.reg_read(UC_X86_REG_RSP)
                (address,) = QWORD.unpack(self.cpu.mem_read(rsp, 8))
                self.cpu.reg_write(UC_X86_REG_RSP, rsp + 8)
                self.cpu.reg_write(UC_X86_REG_RAX, result)
        except (UcError, GuestFault) as error:
            self.blocker = {"reason": "guest CPU stopped", "detail": str(error)}
        pc = self.cpu.reg_read(UC_X86_REG_RIP)
        if self.blocker:
            if self.plan["base"] <= pc < self.plan["base"] + self.plan["size"]:
                self.blocker["pc_rva"] = hex(pc - self.plan["base"])
            return False
        return True

    def run_initializers(self, name, first, end):
        if end < first or (end - first) % 8 or end - first > 65536 * 8:
            self.blocker = {"reason": "invalid CRT initializer table"}
            return 0
        caller = self.cpu.context_save()
        child_sp = ((self.cpu.reg_read(UC_X86_REG_RSP) - 32) & ~15) - 8
        for at in range(first, end, 8):
            (target,) = QWORD.unpack(self.cpu.mem_read(at, 8))
            if not target:
                continue
            self.initializers.append(hex(target - self.plan["base"]))
            if self.log:
                self.log(
                    f"Initialiseur {len(self.initializers)}  RVA {self.initializers[-1]}"
                )
            self.cpu.mem_write(child_sp, QWORD.pack(self.return_pc))
            self.cpu.reg_write(UC_X86_REG_RSP, child_sp)
            if not self.run_until_return(target):
                return 0
            value = self.cpu.reg_read(UC_X86_REG_RAX) & 0xFFFFFFFF
            self.cpu.context_restore(caller)
            if name == "_initterm_e" and value:
                return value
        return 0
