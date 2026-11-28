"""Verified VEX vector operations for the startup reference."""

from functools import lru_cache

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from capstone import x86_const as instructions
from capstone.x86_const import (
    X86_INS_VBROADCASTF128,
    X86_INS_VBROADCASTI128,
    X86_INS_VBROADCASTSD,
    X86_INS_VBROADCASTSS,
    X86_INS_VMOVAPD,
    X86_INS_VMOVAPS,
    X86_INS_VMOVDQA,
    X86_INS_VMOVDQU,
    X86_INS_VMOVUPD,
    X86_INS_VMOVUPS,
    X86_INS_VPBROADCASTB,
    X86_INS_VPBROADCASTD,
    X86_INS_VPBROADCASTQ,
    X86_INS_VPBROADCASTW,
    X86_INS_VZEROUPPER,
    X86_OP_IMM,
    X86_OP_MEM,
    X86_OP_REG,
)
from unicorn import UC_PROT_EXEC, x86_const

from . import fma
from .half import HALF_OPS, execute_half
from .memory import GuestFault, write_guest
from .packed_integer import INTEGER_OPS, execute_integer
from .vector_extra import (
    FLOAT128_OPS,
    INT128_OPS,
    LANE_SELECT_OPS,
    SCALAR_FMA_OPS,
    integer_lanes,
    packed_float,
    scalar_fma,
    select_lanes,
)
from .vector_operands import effective_address, operand_bytes, vector_register, ymm

FMA_OPS = {
    getattr(instructions, f"X86_INS_V{family}{order}PS"): op
    for op, (family, order) in enumerate(
        (family, order)
        for family in ("FMADD", "FMSUB", "FNMADD", "FNMSUB")
        for order in (132, 213, 231)
    )
}

ALIGNED_MOVES = (X86_INS_VMOVAPS, X86_INS_VMOVAPD, X86_INS_VMOVDQA)
BROADCAST_BYTES = {
    X86_INS_VBROADCASTF128: 16,
    X86_INS_VBROADCASTI128: 16,
    X86_INS_VBROADCASTSS: 4,
    X86_INS_VBROADCASTSD: 8,
    X86_INS_VPBROADCASTB: 1,
    X86_INS_VPBROADCASTW: 2,
    X86_INS_VPBROADCASTD: 4,
    X86_INS_VPBROADCASTQ: 8,
}


VECTOR_OPS = frozenset(
    (
        X86_INS_VMOVUPS,
        X86_INS_VMOVUPD,
        *ALIGNED_MOVES,
        X86_INS_VMOVDQU,
        X86_INS_VZEROUPPER,
        *INTEGER_OPS,
        *BROADCAST_BYTES,
        *FMA_OPS,
        *HALF_OPS,
        *SCALAR_FMA_OPS,
        *LANE_SELECT_OPS,
        *FLOAT128_OPS,
        *INT128_OPS,
    )
)


def execute_packed128(cpu, insn):
    destination, first, *rest = insn.operands
    if destination.size != 16:
        return False
    if insn.id == instructions.X86_INS_VPSRAD and rest[-1].type != X86_OP_IMM:
        return False
    first_value = operand_bytes(cpu, insn, first, 16)
    control = rest.pop().imm if rest and rest[-1].type == X86_OP_IMM else 0
    second_value = operand_bytes(cpu, insn, rest[0], 16) if rest else bytes(16)
    if insn.id in FLOAT128_OPS:
        mxcsr = cpu.reg_read(x86_const.UC_X86_REG_MXCSR)
        value, mxcsr = packed_float(
            FLOAT128_OPS[insn.id], first_value, second_value, mxcsr
        )
        cpu.reg_write(x86_const.UC_X86_REG_MXCSR, mxcsr)
    else:
        value = integer_lanes(insn.id, first_value, second_value, control)
    cpu.reg_write(vector_register(insn, destination), int.from_bytes(value, "little"))
    return True


class VectorMoves:
    def __init__(self):
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        decoder.detail = True

        @lru_cache(maxsize=2048)
        def decode(pc, code):
            return next(decoder.disasm(code, pc, count=1), None)

        self.decode = decode

    @staticmethod
    def fetch(cpu, pc):
        # Decode only accessible instruction bytes, including across page boundaries.
        code = bytearray()
        for low, high, flags in sorted(cpu.mem_regions()):
            current = pc + len(code)
            if high < current:
                continue
            if low > current or not flags & UC_PROT_EXEC:
                break
            code.extend(cpu.mem_read(current, min(15 - len(code), high - current + 1)))
            if len(code) == 15:
                break
        return bytes(code)

    def execute(self, cpu, pc):
        return self.execute_fetched(cpu, pc, self.fetch(cpu, pc))

    def execute_fetched(self, cpu, pc, code):
        """Check live instruction bytes from an executable span, then run the operation."""
        insn = self.decode(pc, code)
        if not insn or insn.id not in VECTOR_OPS:
            return None
        # Capstone also names EVEX instructions VMOVUPS. Masked and 512-bit
        # forms have different semantics and must stay with another implementation.
        if insn.encoding.modrm_offset and insn.addr_size != 8:
            return None
        if not code or code[0] not in (0xC4, 0xC5):
            return None
        if insn.id == X86_INS_VZEROUPPER:
            lower = (1 << 128) - 1
            for index in range(16):
                reg = ymm(index)
                cpu.reg_write(reg, cpu.reg_read(reg) & lower)
        elif insn.id in HALF_OPS:
            execute_half(cpu, insn)
        elif insn.id in FMA_OPS or insn.id in SCALAR_FMA_OPS:
            destination, second, third = insn.operands
            width = destination.size
            control = cpu.reg_read(x86_const.UC_X86_REG_MXCSR)
            if insn.id in SCALAR_FMA_OPS:
                value, control = scalar_fma(
                    SCALAR_FMA_OPS[insn.id],
                    operand_bytes(cpu, insn, destination, 16),
                    operand_bytes(cpu, insn, second, 4),
                    operand_bytes(cpu, insn, third, 4),
                    control,
                )
            else:
                operands = (
                    operand_bytes(cpu, insn, operand, width)
                    for operand in (destination, second, third)
                )
                value, control = fma.evaluate(FMA_OPS[insn.id], *operands, control)
            cpu.reg_write(
                vector_register(insn, destination), int.from_bytes(value, "little")
            )
            cpu.reg_write(x86_const.UC_X86_REG_MXCSR, control)
        elif insn.id in FLOAT128_OPS or insn.id in INT128_OPS:
            if not execute_packed128(cpu, insn):
                return None
        elif insn.id in LANE_SELECT_OPS:
            destination, first, second, control = insn.operands
            value = select_lanes(
                insn.id,
                operand_bytes(cpu, insn, first, first.size),
                operand_bytes(cpu, insn, second, second.size),
                operand_bytes(cpu, insn, control, control.size)
                if control.type == X86_OP_REG
                else control.imm,
            )
            cpu.reg_write(
                vector_register(insn, destination), int.from_bytes(value, "little")
            )
        elif insn.id in BROADCAST_BYTES:
            destination, source = insn.operands
            element_width = BROADCAST_BYTES[insn.id]
            element = operand_bytes(cpu, insn, source, element_width)
            value = element * (destination.size // element_width)
            cpu.reg_write(
                vector_register(insn, destination), int.from_bytes(value, "little")
            )
        elif insn.id in INTEGER_OPS:
            execute_integer(cpu, insn)
        else:
            self.move(cpu, insn)
        return pc + insn.size

    @staticmethod
    def move(cpu, insn):
        destination, source = insn.operands
        size = source.size
        if size not in (16, 32) or destination.size != size:
            raise ValueError("unexpected VEX operand size")
        if insn.id in ALIGNED_MOVES:
            for operand in (destination, source):
                if (
                    operand.type == X86_OP_MEM
                    and effective_address(cpu, insn, operand.mem) % size
                ):
                    raise GuestFault(
                        f"misaligned {insn.mnemonic} operand, requires {size} bytes"
                    )
        value = operand_bytes(cpu, insn, source, size)
        if destination.type == X86_OP_MEM:
            write_guest(cpu, effective_address(cpu, insn, destination.mem), value)
        else:
            # VEX.128 clears the upper half of its destination.
            cpu.reg_write(
                vector_register(insn, destination), int.from_bytes(value, "little")
            )
