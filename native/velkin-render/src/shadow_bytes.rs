//! Exact bounds of changes in the common prefix of two byte slices.

pub fn first_difference(a: &[u8], b: &[u8]) -> Option<usize> {
    let n = a.len().min(b.len());
    let (a, a_tail) = a[..n].as_chunks::<8>();
    let (b, b_tail) = b[..n].as_chunks::<8>();
    for (index, (x, y)) in a.iter().zip(b).enumerate() {
        let mask = u64::from_le_bytes(*x) ^ u64::from_le_bytes(*y);
        if mask != 0 {
            return Some(index * 8 + mask.trailing_zeros() as usize / 8);
        }
    }
    a_tail
        .iter()
        .zip(b_tail)
        .position(|(x, y)| x != y)
        .map(|i| a.len() * 8 + i)
}

pub fn last_difference(a: &[u8], b: &[u8]) -> Option<usize> {
    let n = a.len().min(b.len());
    let (a, a_tail) = a[..n].as_chunks::<8>();
    let (b, b_tail) = b[..n].as_chunks::<8>();
    if let Some(i) = a_tail.iter().zip(b_tail).rposition(|(x, y)| x != y) {
        return Some(a.len() * 8 + i);
    }
    for (index, (x, y)) in a.iter().zip(b).enumerate().rev() {
        let mask = u64::from_le_bytes(*x) ^ u64::from_le_bytes(*y);
        if mask != 0 {
            return Some(index * 8 + 7 - mask.leading_zeros() as usize / 8);
        }
    }
    None
}

/// An initialized prefix for a checked read that must succeed before consumption.
pub fn read_scratch(bytes: &mut Vec<u8>, n: usize) -> &mut [u8] {
    if bytes.len() < n {
        bytes.resize(n, 0);
    }
    &mut bytes[..n]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(a: &[u8], b: &[u8]) {
        let pairs = a.iter().zip(b);
        assert_eq!(
            first_difference(a, b),
            pairs.clone().position(|(x, y)| x != y)
        );
        assert_eq!(
            last_difference(a, b),
            pairs.clone().rposition(|(x, y)| x != y)
        );
    }

    #[test]
    fn short_unaligned_spans_match_scalar_for_all_pairs_of_changes() {
        let a = [0x19; 64];
        for n in 0..=39 {
            for offset in 0..8 {
                let a = &a[offset..offset + n];
                let mut b = [0x19; 64];
                let b = &mut b[7 - offset..7 - offset + n];
                check(a, b);
                check(a, &b[..n.saturating_sub(1)]);
                for i in 0..n {
                    for j in i..n {
                        b[i] ^= 1;
                        b[j] ^= 128;
                        check(a, b);
                        b[i] ^= 1;
                        b[j] ^= 128;
                    }
                }
            }
        }
    }

    #[test]
    fn reads_reuse_initialized_prefixes() {
        let mut bytes = Vec::new();
        assert!(read_scratch(&mut bytes, 0).is_empty());
        assert_eq!(bytes.capacity(), 0);
        read_scratch(&mut bytes, 23).fill(7);
        let pointer = bytes.as_ptr();
        assert_eq!(read_scratch(&mut bytes, 8), &[7; 8]);
        assert_eq!(read_scratch(&mut bytes, 23), &[7; 23]);
        assert_eq!(bytes.as_ptr(), pointer);
        assert_eq!(read_scratch(&mut bytes, 31)[23..], [0; 8]);
    }

    #[cfg(windows)]
    #[test]
    fn private_mappings_end_at_an_inaccessible_page() {
        use std::ffi::c_void;
        use std::ptr::null_mut;
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn VirtualAlloc(addr: *mut c_void, size: usize, kind: u32, protect: u32)
            -> *mut c_void;
            fn VirtualFree(addr: *mut c_void, size: usize, kind: u32) -> i32;
        }
        const PAGE: usize = 4096; // x64 Windows base pages.
        struct Private(*mut c_void);
        impl Drop for Private {
            fn drop(&mut self) {
                // SAFETY: release exactly the reservation returned by VirtualAlloc.
                unsafe { VirtualFree(self.0, 0, 0x8000) };
            }
        }
        fn reserve() -> Private {
            // SAFETY: reserve two private pages and commit only the first.
            let base = unsafe { VirtualAlloc(null_mut(), 2 * PAGE, 0x2000, 1) };
            assert!(!base.is_null());
            let mapping = Private(base);
            assert_eq!(unsafe { VirtualAlloc(base, PAGE, 0x1000, 4) }, base);
            mapping
        }
        let (a, b) = (reserve(), reserve());
        // SAFETY: independent, committed, writable, zero-initialized first pages.
        let a = unsafe { std::slice::from_raw_parts(a.0.cast::<u8>(), PAGE) };
        let b = unsafe { std::slice::from_raw_parts_mut(b.0.cast::<u8>(), PAGE) };
        for n in 8..=31 {
            let (a, b) = (&a[PAGE - n..], &mut b[PAGE - n..]);
            check(a, b);
            for i in 0..n {
                b[i] = 1;
                check(a, b);
                b[i] = 0;
            }
        }
    }
}
