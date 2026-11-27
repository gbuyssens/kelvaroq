use std::arch::asm;

#[repr(C)]
pub struct Packed128 {
    first: [u32; 4],
    second: [u32; 4],
    mxcsr: u32,
}

/// Evaluate packed signed dword conversion (0) or binary32 subtraction (1).
///
/// # Safety
/// The caller must supply an initialized, aligned and writable `Packed128`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn velkin_packed128(op: u32, block: *mut Packed128) -> u32 {
    let Some(block) = (unsafe { block.as_mut() }) else {
        return 1;
    };
    if block.mxcsr >> 16 != 0 || block.mxcsr & 0x1f80 != 0x1f80 {
        return 3;
    }
    let mut saved = 0u32;
    macro_rules! evaluate {
        ($instruction:literal) => {{
            // SAFETY: SSE2 is part of x86-64. Only validated, masked FP state
            // and the two fixed-size buffers enter the arithmetic operation.
            unsafe {
                asm!(
                    "stmxcsr [{saved}]", "ldmxcsr [{control}]",
                    "movups xmm0, [{first}]", "movups xmm1, [{second}]",
                    $instruction,
                    "movups [{first}], xmm0", "stmxcsr [{control}]", "ldmxcsr [{saved}]",
                    saved = in(reg) &mut saved, control = in(reg) &mut block.mxcsr,
                    first = in(reg) block.first.as_mut_ptr(), second = in(reg) block.second.as_ptr(),
                    out("xmm0") _, out("xmm1") _, options(nostack, preserves_flags)
                );
            }
        }};
    }
    match op {
        0 => evaluate!("cvtdq2ps xmm0, xmm0"),
        1 => evaluate!("subps xmm0, xmm1"),
        _ => return 1,
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subtraction_records_invalid_and_restores_host_mxcsr() {
        let host_mxcsr = || {
            let mut value = 0u32;
            // SAFETY: STMXCSR writes one valid, four-byte local.
            unsafe { asm!("stmxcsr [{}]", in(reg) &mut value, options(nostack, preserves_flags)) };
            value
        };
        let before = host_mxcsr();
        let mut state = Packed128 {
            first: [0x7f80_0001, 0x8000_0000, 0, 0],
            second: [0; 4],
            mxcsr: 0x1f80,
        };
        assert_eq!(unsafe { velkin_packed128(1, &mut state) }, 0);
        assert_eq!(state.first, [0x7fc0_0001, 0x8000_0000, 0, 0]);
        assert_eq!(state.mxcsr, 0x1f81);
        assert_eq!(host_mxcsr(), before);
    }
}
