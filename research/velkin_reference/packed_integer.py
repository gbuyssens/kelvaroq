"""AVX2 signed comparison, saturation and word shuffles within 128-bit lanes."""

import operator
import struct

from capstone import x86_const as ins
from unicorn import UC_PROT_WRITE

from .memory import check_access, read_guest, write_guest
from .vector_operands import effective_address, operand_bytes, write_result

PACK = {
    ins.X86_INS_VPACKSSDW: ("i", "h", 32767),
    ins.X86_INS_VPACKSSWB: ("h", "b", 127),
}
SHUFFLE = {
    ins.X86_INS_VPSHUFLW: ("H", 0),
    ins.X86_INS_VPSHUFHW: ("H", 4),
    ins.X86_INS_VPSHUFD: ("I", 0),
}
BITWISE = {
    ins.X86_INS_VXORPS: operator.xor,
    ins.X86_INS_VPXOR: operator.xor,
    ins.X86_INS_VPOR: operator.or_,
}
ARITHMETIC = {
    ins.X86_INS_VPADDQ: ("Q", operator.add),
    ins.X86_INS_VPSUBQ: ("Q", operator.sub),
    ins.X86_INS_VPADDW: ("H", operator.add),
    ins.X86_INS_VPSUBW: ("H", operator.sub),
    ins.X86_INS_VPADDD: ("I", operator.add),
    ins.X86_INS_VPSUBD: ("I", operator.sub),
}
COMPARE = {
    ins.X86_INS_VPCMPGTQ: ("q", operator.gt),
    ins.X86_INS_VPCMPEQD: ("i", operator.eq),
}
WIDEN = {
    ins.X86_INS_VPMOVSXBQ: ("b", "q"),
    ins.X86_INS_VPMOVSXBW: ("b", "h"),
    ins.X86_INS_VPMOVSXBD: ("b", "i"),
    ins.X86_INS_VPMOVZXWD: ("H", "I"),
}
EXTRACT = {
    ins.X86_INS_VPEXTRB: 1,
    ins.X86_INS_VPEXTRW: 2,
    ins.X86_INS_VPEXTRD: 4,
    ins.X86_INS_VEXTRACTI128: 16,
}
SIGNED_SELECT = {ins.X86_INS_VPMINSD: min, ins.X86_INS_VPMAXSD: max}
MOVE = {ins.X86_INS_VMOVD: 4, ins.X86_INS_VMOVQ: 8}
MASKED_MOVES = {ins.X86_INS_VPMASKMOVD: 4, ins.X86_INS_VPMASKMOVQ: 8}
INTEGER_OPS = frozenset(
    (
        *BITWISE,
        *ARITHMETIC,
        *PACK,
        *SHUFFLE,
        *WIDEN,
        *COMPARE,
        *MOVE,
        *EXTRACT,
        *SIGNED_SELECT,
        ins.X86_INS_VPERMQ,
        ins.X86_INS_VPSLLD,
        ins.X86_INS_VPSLLVD,
        *MASKED_MOVES,
    )
)


def pack_signed(op, first, second):
    source, dest, maximum = PACK[op]
    result = bytearray()
    for lane in range(0, len(first), 16):
        for value in (first, second):
            for (n,) in struct.iter_unpack("<" + source, value[lane : lane + 16]):
                result.extend(
                    struct.pack("<" + dest, max(-maximum - 1, min(maximum, n)))
                )
    return bytes(result)


def masked_move(cpu, instruction):
    size = MASKED_MOVES[instruction.id]
    dst, mask, src = instruction.operands
    storing = dst.type == ins.X86_OP_MEM
    mem = dst if storing else src
    width = mask.size
    base = effective_address(cpu, instruction, mem.mem)
    bits = operand_bytes(cpu, instruction, mask, width)
    offsets = [i for i in range(0, width, size) if bits[i + size - 1] & 0x80]
    if storing:
        data = operand_bytes(cpu, instruction, src, width)
        for i in offsets:
            check_access(cpu, base + i, size, UC_PROT_WRITE)
        for i in offsets:
            write_guest(cpu, base + i, data[i : i + size])
    else:
        data = bytearray(width)
        for i in offsets:
            data[i : i + size] = read_guest(cpu, base + i, size)
        write_result(cpu, instruction, dst, data)


def binary_lanes(op, a, b):
    if op in BITWISE:
        bits = BITWISE[op](int.from_bytes(a, "little"), int.from_bytes(b, "little"))
        return bits.to_bytes(len(a), "little")
    if op in PACK:
        return pack_signed(op, a, b)
    if op in SIGNED_SELECT:
        select = SIGNED_SELECT[op]
        layout = struct.Struct("<" + "i" * (len(a) // 4))
        return layout.pack(
            *(select(x, y) for x, y in zip(layout.unpack(a), layout.unpack(b)))
        )
    comparing = op in COMPARE
    element, operation = (COMPARE if comparing else ARITHMETIC)[op]
    size = struct.calcsize("<" + element)
    layout = struct.Struct("<" + element * (len(a) // size))
    results = (operation(x, y) for x, y in zip(layout.unpack(a), layout.unpack(b)))
    if comparing:
        return layout.pack(*(-int(result) for result in results))
    mask = (1 << (size * 8)) - 1
    return layout.pack(*(result & mask for result in results))


def execute_integer(cpu, instruction):
    op = instruction.id
    dst, src, *extra = instruction.operands
    width = dst.size

    def read(operand, size):
        return operand_bytes(cpu, instruction, operand, size)

    if op in MASKED_MOVES:
        masked_move(cpu, instruction)
        return
    if op in MOVE:
        data = read(src, MOVE[op])
    elif op in EXTRACT:
        size = EXTRACT[op]
        offset = (extra[0].imm & (src.size // size - 1)) * size
        data = read(src, src.size)[offset : offset + size]
    elif op in WIDEN:
        source, target = WIDEN[op]
        count = width // struct.calcsize("<" + target)
        raw = read(src, count * struct.calcsize("<" + source))
        data = struct.pack(
            f"<{count}{target}", *struct.unpack(f"<{count}{source}", raw)
        )
    elif op in SHUFFLE:
        data = shuffle_words(op, read(src, width), extra[0].imm)
    elif op == ins.X86_INS_VPERMQ:
        lanes = struct.unpack("<4Q", read(src, 32))
        data = struct.pack(
            "<4Q", *(lanes[(extra[0].imm >> shift) & 3] for shift in (0, 2, 4, 6))
        )
    elif op in (ins.X86_INS_VPSLLD, ins.X86_INS_VPSLLVD):
        shift = extra[0]
        layout = struct.Struct(f"<{width // 4}I")
        if op == ins.X86_INS_VPSLLVD:
            counts = layout.unpack(read(shift, width))
        else:
            count = (
                shift.imm
                if shift.type == ins.X86_OP_IMM
                else int.from_bytes(read(shift, 16)[:8], "little")
            )
            counts = (count,) * (width // 4)
        values = layout.unpack(read(src, width))
        data = layout.pack(
            *((n << c) & 0xFFFFFFFF if c < 32 else 0 for n, c in zip(values, counts))
        )
    else:
        data = binary_lanes(op, read(src, width), read(extra[0], width))
    write_result(cpu, instruction, dst, data)


def shuffle_words(op, value, imm):
    element, start = SHUFFLE[op]
    layout = struct.Struct("<" + element * (16 // struct.calcsize("<" + element)))
    result = bytearray()
    for lane in range(0, len(value), 16):
        words = list(layout.unpack_from(value, lane))
        selected = [words[start + ((imm >> (2 * n)) & 3)] for n in range(4)]
        words[start : start + 4] = selected
        result.extend(layout.pack(*words))
    return bytes(result)
