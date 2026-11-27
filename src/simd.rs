//! Fixed x64 arithmetic operations for the reference CPU. Only operand data
//! crosses this ABI. The guest's FP environment is isolated from the caller.

#[cfg(windows)]
mod launcher_http;

#[cfg(windows)]
mod adapters;
#[cfg(windows)]
mod hardware;

#[cfg(target_arch = "x86_64")]
#[path = "simd_half.rs"]
mod simd_half;

#[cfg(target_arch = "x86_64")]
#[path = "simd_vector.rs"]
mod simd_vector;

#[cfg(target_arch = "x86_64")]
use std::arch::asm;

#[repr(C)]
pub struct FmaBlock {
    dst: [u32; 4],
    second: [u32; 4],
    third: [u32; 4],
    mxcsr: u32,
}

#[cfg(all(windows, target_arch = "x86_64"))]
#[link(name = "ucrt")]
unsafe extern "C" {
    fn tanf(value: f32) -> f32;
    fn atan2f(y: f32, x: f32) -> f32;
    fn cosf(value: f32) -> f32;
    fn sinf(value: f32) -> f32;
    fn exp2f(value: f32) -> f32;
    fn acosf(value: f32) -> f32;
    fn asinf(value: f32) -> f32;
    fn atanf(value: f32) -> f32;
    fn cbrtf(value: f32) -> f32;
    fn ceilf(value: f32) -> f32;
    fn erff(value: f32) -> f32;
    fn expf(value: f32) -> f32;
    fn fmodf(first: f32, second: f32) -> f32;
    fn log10f(value: f32) -> f32;
    fn log2f(value: f32) -> f32;
    fn logf(value: f32) -> f32;
    fn nextafterf(first: f32, second: f32) -> f32;
    fn powf(first: f32, second: f32) -> f32;
    fn remainderf(first: f32, second: f32) -> f32;
    fn sinhf(value: f32) -> f32;
    fn _errno() -> *mut i32;
}

#[repr(C)]
pub struct MathF32 {
    value: u32,
    mxcsr: u32,
    error: i32,
}

/// Call the Windows binary32 CRT with a private FP environment.
///
/// # Safety
/// `state` must be aligned, initialized and writable for the whole call.
#[cfg(all(windows, target_arch = "x86_64"))]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn velkin_tanf(state: *mut MathF32) -> u32 {
    // SAFETY: retain the original entry point and its pointer contract.
    unsafe { velkin_math_f32(0, 0, state) }
}

/// Evaluate a Windows CRT float operation and isolate the guest errno and MXCSR.
///
/// # Safety
/// `state` must be aligned, initialized and writable for the whole call.
#[cfg(all(windows, target_arch = "x86_64"))]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn velkin_math_f32(op: u32, second: u32, state: *mut MathF32) -> u32 {
    enum Operation {
        Unary(unsafe extern "C" fn(f32) -> f32),
        Binary(unsafe extern "C" fn(f32, f32) -> f32),
    }
    use Operation::{Binary, Unary};
    // Stable operation numbers shared with the Python bridge.
    const OPERATIONS: &[Operation] = &[
        Unary(tanf),
        Binary(atan2f),
        Unary(cosf),
        Unary(sinf),
        Unary(exp2f),
        Unary(acosf),
        Unary(asinf),
        Unary(atanf),
        Unary(cbrtf),
        Unary(ceilf),
        Unary(erff),
        Unary(expf),
        Binary(fmodf),
        Unary(log10f),
        Unary(log2f),
        Unary(logf),
        Binary(nextafterf),
        Binary(powf),
        Binary(remainderf),
        Unary(sinhf),
    ];
    let Some(state) = (unsafe { state.as_mut() }) else {
        return 1;
    };
    let Some(operation) = OPERATIONS.get(op as usize) else {
        return 1;
    };
    if state.mxcsr >> 16 != 0 || state.mxcsr & 0x1f80 != 0x1f80 {
        return 3;
    }
    let mut host_mxcsr = 0u32;
    // SAFETY: _errno() returns thread-local storage. Save and restore both
    // the host errno and FP state around the single external CRT operation.
    unsafe {
        let errno = _errno();
        let saved_errno = *errno;
        *errno = 0;
        asm!("stmxcsr [{saved}]", "ldmxcsr [{guest}]",
             saved = in(reg) &mut host_mxcsr, guest = in(reg) &state.mxcsr,
             options(nostack, preserves_flags));
        let first = f32::from_bits(state.value);
        state.value = match operation {
            Unary(function) => function(first),
            Binary(function) => function(first, f32::from_bits(second)),
        }
        .to_bits();
        asm!("stmxcsr [{guest}]", "ldmxcsr [{saved}]",
             saved = in(reg) &host_mxcsr, guest = in(reg) &mut state.mxcsr,
             options(nostack, preserves_flags));
        state.error = *errno;
        *errno = saved_errno;
    }
    0
}

/// Evaluate four binary32 lanes and return the updated guest MXCSR in place.
///
/// # Safety
/// `block` must point to a writable, aligned FmaBlock for the entire call.
#[cfg(target_arch = "x86_64")]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn velkin_fma_ps(op: u32, block: *mut FmaBlock) -> u32 {
    // SAFETY: the only caller supplies a private, initialized FmaBlock.
    let Some(block) = (unsafe { block.as_mut() }) else {
        return 1;
    };
    if !is_x86_feature_detected!("fma") || !is_x86_feature_detected!("avx") {
        return 2;
    }
    if block.mxcsr >> 16 != 0 || block.mxcsr & 0x1f80 != 0x1f80 {
        return 3; // Unmasked faults require guest exception delivery.
    }
    let mut saved = 0u32;
    macro_rules! evaluate {
        ($instruction:literal) => {{
            // SAFETY: CPU support, trap masks and reserved bits were checked.
            // All buffers live across the block and only explicit XMM registers
            // are clobbered. Restore the host FP control word before returning.
            unsafe {
                asm!(
                    "stmxcsr [{saved}]", "ldmxcsr [{control}]",
                    "vmovups xmm0, [{dst}]", "vmovups xmm1, [{second}]", "vmovups xmm2, [{third}]",
                    concat!($instruction, " xmm0, xmm1, xmm2"),
                    "vmovups [{dst}], xmm0", "stmxcsr [{control}]", "ldmxcsr [{saved}]",
                    saved = in(reg) &mut saved, control = in(reg) &mut block.mxcsr,
                    dst = in(reg) block.dst.as_mut_ptr(), second = in(reg) block.second.as_ptr(),
                    third = in(reg) block.third.as_ptr(),
                    out("xmm0") _, out("xmm1") _, out("xmm2") _, options(nostack, preserves_flags)
                );
            }
        }};
    }
    match op {
        0 => evaluate!("vfmadd132ps"),
        1 => evaluate!("vfmadd213ps"),
        2 => evaluate!("vfmadd231ps"),
        3 => evaluate!("vfmsub132ps"),
        4 => evaluate!("vfmsub213ps"),
        5 => evaluate!("vfmsub231ps"),
        6 => evaluate!("vfnmadd132ps"),
        7 => evaluate!("vfnmadd213ps"),
        8 => evaluate!("vfnmadd231ps"),
        9 => evaluate!("vfnmsub132ps"),
        10 => evaluate!("vfnmsub213ps"),
        11 => evaluate!("vfnmsub231ps"),
        _ => return 1,
    }
    0
}

#[cfg(all(test, target_arch = "x86_64"))]
mod tests {
    use super::*;

    fn control_word() -> u32 {
        let mut value = 0;
        // SAFETY: STMXCSR stores four bytes in the local variable.
        unsafe { asm!("stmxcsr [{}]", in(reg) &mut value, options(nostack, preserves_flags)) };
        value
    }

    fn block(a: f32, b: f32, c: f32, mxcsr: u32) -> FmaBlock {
        FmaBlock {
            dst: [a.to_bits(); 4],
            second: [b.to_bits(); 4],
            third: [c.to_bits(); 4],
            mxcsr,
        }
    }

    #[test]
    fn all_fma_orders_and_signs() {
        if !is_x86_feature_detected!("fma") {
            return;
        }
        let expected: [f32; 12] = [
            17., 13., 23., 11., -1., 19., -11., 1., -19., -17., -13., -23.,
        ];
        for (op, value) in expected.iter().enumerate() {
            let mut state = block(2., 3., 7., 0x1f80);
            assert_eq!(unsafe { velkin_fma_ps(op as u32, &mut state) }, 0);
            assert_eq!(state.dst, [value.to_bits(); 4]);
        }
    }

    #[test]
    fn fused_cancellation_rounding_and_host_isolation() {
        if !is_x86_feature_detected!("fma") {
            return;
        }
        let before = control_word();
        let mut state = block(
            f32::from_bits(0x3f80_0001),
            f32::from_bits(0x3f7f_fffe),
            1.,
            0x1f80,
        );
        assert_eq!(unsafe { velkin_fma_ps(4, &mut state) }, 0);
        assert_eq!(state.dst, [0xa880_0000; 4]);
        let mut state = block(f32::from_bits(0x3380_0000), 1., 1., 0x5f80);
        assert_eq!(unsafe { velkin_fma_ps(1, &mut state) }, 0);
        assert_eq!(state.dst, [0x3f80_0001; 4]);
        assert_eq!(state.mxcsr, 0x5fa0);
        assert_eq!(control_word(), before);
        state.mxcsr = 0;
        let data = state.dst;
        assert_eq!(unsafe { velkin_fma_ps(1, &mut state) }, 3);
        assert_eq!(state.dst, data);
        assert_eq!(control_word(), before);
    }

    #[cfg(windows)]
    #[test]
    fn tan_preserves_negative_zero_and_host_state() {
        let before = control_word();
        let mut state = MathF32 {
            value: (-0.0f32).to_bits(),
            mxcsr: 0x1f80,
            error: 0,
        };
        assert_eq!(unsafe { velkin_tanf(&mut state) }, 0);
        assert_eq!(state.value, (-0.0f32).to_bits());
        assert_eq!(state.error, 0);
        assert_eq!(control_word(), before);
    }

    #[cfg(windows)]
    #[test]
    fn crt_domain_error_preserves_host_errno_and_fp_control() {
        let mut state = MathF32 {
            value: f32::INFINITY.to_bits(),
            mxcsr: 0x3f84,
            error: 0,
        };
        let before = control_word();
        // SAFETY: the pointer belongs to the current thread. Restore its previous errno.
        let (status, errno_after) = unsafe {
            let errno = _errno();
            let saved = *errno;
            *errno = 71;
            let status = velkin_math_f32(3, 0, &mut state);
            let errno_after = *errno;
            *errno = saved;
            (status, errno_after)
        };
        assert_eq!(status, 0);
        assert_eq!(errno_after, 71);
        assert_eq!(control_word(), before);
        assert!(f32::from_bits(state.value).is_nan());
        assert_eq!(state.error, 33); // Windows EDOM
        assert_eq!(state.mxcsr, 0x3f85); // preserve prior divide-by-zero; raise invalid
    }
}
