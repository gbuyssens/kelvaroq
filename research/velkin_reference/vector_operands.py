"""Access decoded x64 operands with guest permissions and VEX write widths."""

from capstone.x86_const import X86_OP_MEM, X86_OP_REG, X86_REG_RIP
from unicorn import x86_const

from .memory import read_guest, write_guest

DWORD_REGISTERS = {
    f"e{part}": f"r{part}" for part in ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp")
}
DWORD_REGISTERS.update({f"r{i}d": f"r{i}" for i in range(8, 16)})


def ymm(index):
    return getattr(x86_const, f"UC_X86_REG_YMM{index}")


def vector_register(insn, operand):
    name = insn.reg_name(operand.reg)
    if operand.type != X86_OP_REG or not name.startswith(("xmm", "ymm")):
        raise ValueError("expected an x86 vector register")
    return ymm(int(name[3:]))


def effective_address(cpu, insn, mem):
    def read(reg):
        if not reg:
            return 0
        if reg == X86_REG_RIP:
            return insn.address + insn.size
        name = insn.reg_name(reg).upper()
        return cpu.reg_read(getattr(x86_const, f"UC_X86_REG_{name}"))

    address = read(mem.base) + read(mem.index) * mem.scale + mem.disp
    segment = insn.reg_name(mem.segment) if mem.segment else None
    if segment in ("fs", "gs"):
        address += cpu.reg_read(
            getattr(x86_const, f"UC_X86_REG_{segment.upper()}_BASE")
        )
    return address & ((1 << 64) - 1)


def operand_bytes(cpu, insn, operand, width):
    if operand.type == X86_OP_MEM:
        return read_guest(cpu, effective_address(cpu, insn, operand.mem), width)
    name = insn.reg_name(operand.reg)
    if name.startswith(("xmm", "ymm")):
        reg = vector_register(insn, operand)
    else:
        reg = getattr(x86_const, "UC_X86_REG_" + name.upper())
    number = cpu.reg_read(reg)
    return number.to_bytes(32, "little")[:width]


def write_result(cpu, insn, operand, data):
    if operand.type == X86_OP_MEM:
        write_guest(cpu, effective_address(cpu, insn, operand.mem), data)
        return
    name = insn.reg_name(operand.reg)
    if name.startswith(("xmm", "ymm")):
        reg = vector_register(insn, operand)
    else:
        # VEX scalar extraction zero-extends a GPR result, including EAX..R15D.
        reg = getattr(
            x86_const, "UC_X86_REG_" + DWORD_REGISTERS.get(name, name).upper()
        )
    cpu.reg_write(reg, int.from_bytes(data, "little"))
