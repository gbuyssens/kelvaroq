//! Count covered bytes in a texture using one residency sample per 64 KiB block.

const BLOCK_BYTES: usize = 64 * 1024;
const PAGE_BYTES: usize = 4096;
pub const MAX_BATCH: usize = 256;

/// Ask for the same page in each block as individual 64 KiB probes would:
/// the page containing the first byte of the texture in that block. Each
/// returned flag applies only to its own address, including across holes.
pub fn mapped_block_bytes(
    start: usize,
    length: usize,
    mut probe: impl FnMut(&[usize], &mut [bool]),
) -> u64 {
    let end = start.saturating_add(length);
    if start == end {
        return 0;
    }
    let mut addresses = [0; MAX_BATCH];
    let mut present = [false; MAX_BATCH];
    let mut block = start & !(BLOCK_BYTES - 1);
    let mut total = 0;
    while block < end {
        let mut count = 0;
        while count < MAX_BATCH && block < end {
            addresses[count] = block.max(start) & !(PAGE_BYTES - 1);
            count += 1;
            block = block.saturating_add(BLOCK_BYTES);
        }
        present[..count].fill(false);
        probe(&addresses[..count], &mut present[..count]);
        for (&address, &mapped) in addresses[..count].iter().zip(&present[..count]) {
            if mapped {
                let base = address & !(BLOCK_BYTES - 1);
                total += (base.saturating_add(BLOCK_BYTES).min(end) - base.max(start)) as u64;
            }
        }
    }
    total
}

/// Placement estimate for a heap slot. As before, two missing endpoints
/// defer a multi-block texture. The first sample also covers its own block.
/// This estimate is never a license to dereference title memory.
pub fn heap_mapped_bytes(
    start: usize,
    length: usize,
    mut probe: impl FnMut(&[usize], &mut [bool]),
) -> u64 {
    let end = start.saturating_add(length);
    if start == end {
        return 0;
    }
    let first_end = (start | (BLOCK_BYTES - 1)).saturating_add(1).min(end);
    let mut present = [false];
    probe(&[start & !(PAGE_BYTES - 1)], &mut present);
    let first_bytes = if present[0] {
        (first_end - start) as u64
    } else {
        0
    };
    if first_end == end {
        return first_bytes;
    }
    if !present[0] {
        probe(&[(end - 1) & !(PAGE_BYTES - 1)], &mut present);
        if !present[0] {
            return 0;
        }
    }
    first_bytes + mapped_block_bytes(first_end, end - first_end, probe)
}

/// A known partial placement is revisited after one frame regardless of
/// staging residency. Other textures keep their full page-level check.
pub fn heap_texture_revisit(
    frame: u64,
    partial_bytes: Option<u64>,
    fully_mapped: impl FnOnce() -> bool,
) -> Option<(u64, u64)> {
    match partial_bytes {
        Some(bytes) => Some((frame + 1, bytes)),
        None if !fully_mapped() => Some((frame + 120, 0)),
        None => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heap_placement_matches_independent_probes_for_every_three_block_pattern() {
        let start = 13 * PAGE_BYTES + 7;
        for length in [
            0,
            47,
            BLOCK_BYTES - start,
            BLOCK_BYTES + 11,
            2 * BLOCK_BYTES + PAGE_BYTES,
        ] {
            for mask in 0u32..8 {
                let present = |address: usize| mask & (1u32 << (address / BLOCK_BYTES)) != 0;
                let mut seen = Vec::new();
                let result = heap_mapped_bytes(start, length, |addresses, mapped| {
                    seen.extend_from_slice(addresses);
                    for (&address, out) in addresses.iter().zip(mapped) {
                        *out = present(address);
                    }
                });
                let end = start + length;
                let mut expected = 0;
                if length > 0 && (present(start) || present(end - 1)) {
                    for at in ((start & !(BLOCK_BYTES - 1))..end).step_by(BLOCK_BYTES) {
                        if present(at.max(start)) {
                            expected += end.min(at + BLOCK_BYTES) - start.max(at);
                        }
                    }
                }
                assert_eq!(result, expected as u64);
                if length != 0 {
                    assert_eq!(seen.iter().filter(|&&at| at == start - 7).count(), 1);
                } else {
                    assert!(seen.is_empty());
                }
            }
        }
    }

    #[test]
    fn heap_revisit_checks_full_residency_only_if_it_changes_the_deadline() {
        assert_eq!(
            heap_texture_revisit(9, Some(71), || panic!("unused probe")),
            Some((10, 71))
        );
        assert_eq!(heap_texture_revisit(9, None, || false), Some((129, 0)));
        assert_eq!(heap_texture_revisit(9, None, || true), None);
    }

    #[test]
    fn clips_each_end_and_keeps_holes_across_batches() {
        let start = BLOCK_BYTES + 3 * PAGE_BYTES + 19;
        let end = (MAX_BATCH + 7) * BLOCK_BYTES + 71;
        let mut batches = Vec::new();
        let mut observed = Vec::new();
        let choose = |block: usize| block % 5 != 2;
        let result = mapped_block_bytes(start, end - start, |addresses, mapped| {
            batches.push(addresses.len());
            observed.extend_from_slice(addresses);
            for (&address, value) in addresses.iter().zip(mapped) {
                *value = choose(address / BLOCK_BYTES);
            }
        });
        let first = start / BLOCK_BYTES;
        let last = (end - 1) / BLOCK_BYTES;
        let expected: usize = (first..=last)
            .filter(|&block| choose(block))
            .map(|block| end.min((block + 1) * BLOCK_BYTES) - start.max(block * BLOCK_BYTES))
            .sum();
        assert_eq!(result, expected as u64);
        assert_eq!(batches, [MAX_BATCH, last - first + 1 - MAX_BATCH]);
        assert_eq!(observed[0], start - 19);
        assert_eq!(
            observed[1..],
            ((first + 1)..=last)
                .map(|b| b * BLOCK_BYTES)
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn empty_ranges_skip_the_probe_and_overflow_clips_the_last_block() {
        assert_eq!(mapped_block_bytes(17, 0, |_, _| panic!("empty range")), 0);
        assert_eq!(
            mapped_block_bytes(usize::MAX, 1, |_, _| panic!("empty range")),
            0
        );
        assert_eq!(
            mapped_block_bytes(usize::MAX - 17, 128, |addresses, mapped| {
                assert_eq!(addresses, &[usize::MAX & !(PAGE_BYTES - 1)]);
                mapped[0] = true;
            }),
            17
        );
    }
}
