use std::arch::asm;

#[repr(C)]
pub struct HalfBlock {
    lanes: [u8; 32],
    mxcsr: u32,
}

/// Convert binary32 to binary16 (0) or binary16 to binary32 (1) with F16C.
/// Four-lane callers zero-fill unused inputs so they add no FP exceptions.
///
/// # Safety
/// The caller supplies a writable, aligned and initialized `HalfBlock`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn velkin_half(op: u32, imm: u8, state: *mut HalfBlock) -> u32 {
    let Some(state) = (unsafe { state.as_mut() }) else {
        return 1;
    };
    if !is_x86_feature_detected!("avx") || !is_x86_feature_detected!("f16c") {
        return 2;
    }
    if state.mxcsr >> 16 != 0 || state.mxcsr & 0x1f80 != 0x1f80 {
        return 3;
    }
    let mut saved = 0u32;
    macro_rules! convert {
        ($instruction:literal) => {{
            // SAFETY: the host supports F16C and the guest traps are masked.
            // Save and restore thread-local FP controls within one assembly block.
            unsafe {
                asm!(
                    "stmxcsr [{host}]", "ldmxcsr [{guest}]", "vmovups ymm0, [{lanes}]",
                    $instruction,
                    "vmovups [{lanes}], ymm0", "stmxcsr [{guest}]", "ldmxcsr [{host}]",
                    host = in(reg) &mut saved, guest = in(reg) &mut state.mxcsr,
                    lanes = in(reg) state.lanes.as_mut_ptr(),
                    out("ymm0") _, options(nostack, preserves_flags)
                );
            }
        }};
    }
    match op {
        0 => match imm & 7 {
            0 => convert!("vcvtps2ph xmm0, ymm0, 0"),
            1 => convert!("vcvtps2ph xmm0, ymm0, 1"),
            2 => convert!("vcvtps2ph xmm0, ymm0, 2"),
            3 => convert!("vcvtps2ph xmm0, ymm0, 3"),
            _ => convert!("vcvtps2ph xmm0, ymm0, 4"),
        },
        1 => convert!("vcvtph2ps ymm0, xmm0"),
        _ => return 1,
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn half_conversion_restores_host_mxcsr_on_success_and_refusal() {
        if !is_x86_feature_detected!("avx") || !is_x86_feature_detected!("f16c") {
            return;
        }
        let host_mxcsr = || {
            let mut value = 0u32;
            // SAFETY: STMXCSR writes one valid, four-byte local.
            unsafe { asm!("stmxcsr [{}]", in(reg) &mut value, options(nostack, preserves_flags)) };
            value
        };
        let mut state = HalfBlock {
            lanes: [0; 32],
            mxcsr: 0x5f80,
        };
        state.lanes[..4].copy_from_slice(&0x3f80_1000u32.to_le_bytes());
        let before = host_mxcsr();
        assert_eq!(unsafe { velkin_half(0, 4, &mut state) }, 0);
        assert_eq!(&state.lanes[..2], &0x3c01u16.to_le_bytes());
        assert_eq!(state.mxcsr, 0x5fa0);
        assert_eq!(host_mxcsr(), before);
        let bits = state.lanes;
        state.mxcsr = 0;
        assert_eq!(unsafe { velkin_half(1, 0, &mut state) }, 3);
        assert_eq!(state.lanes, bits);
        assert_eq!(state.mxcsr, 0);
        assert_eq!(host_mxcsr(), before);
    }
}
