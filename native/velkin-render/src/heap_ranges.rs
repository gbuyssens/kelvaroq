//! Ranges of a fixed-size memory heap, given out first-fit at an alignment
//! and taken back with their neighbours merged.
//!
//! The host made a committed resource for each texture it uploads: one
//! allocation of the driver's each time, 0.3-0.5 ms on the RTX 5080 for a
//! 2048 x 2048 texture, hundreds a frame while a scene streams in. Textures
//! placed in a few large heaps cost a placement instead. The caller gives a
//! range back only once nothing can still read the resource placed there.

use std::collections::BTreeMap;

/// Free ranges of one heap, by offset.
pub struct HeapRanges {
    size: u64,
    free: BTreeMap<u64, u64>,
    used: u64,
}

impl HeapRanges {
    pub fn new(size: u64) -> Self {
        let mut free = BTreeMap::new();
        if size > 0 {
            free.insert(0, size);
        }
        Self {
            size,
            free,
            used: 0,
        }
    }

    pub fn size(&self) -> u64 {
        self.size
    }

    /// Bytes given out.
    pub fn used(&self) -> u64 {
        self.used
    }

    /// The offset of `bytes` at `align` (a power of two), first fit.
    pub fn alloc(&mut self, bytes: u64, align: u64) -> Option<u64> {
        if bytes == 0 || !align.is_power_of_two() {
            return None;
        }
        let (start, len, at) = self.free.iter().find_map(|(&start, &len)| {
            let at = start.checked_next_multiple_of(align)?;
            (at.checked_add(bytes)? <= start + len).then_some((start, len, at))
        })?;
        self.free.remove(&start);
        if at > start {
            self.free.insert(start, at - start);
        }
        let end = at + bytes;
        if end < start + len {
            self.free.insert(end, start + len - end);
        }
        self.used += bytes;
        Some(at)
    }

    /// Takes back `[at, at + bytes)`, given out by [`HeapRanges::alloc`].
    pub fn release(&mut self, at: u64, bytes: u64) {
        debug_assert!(at + bytes <= self.size);
        let (mut start, mut end) = (at, at + bytes);
        if let Some((&before, &len)) = self.free.range(..start).next_back()
            && before + len == start
        {
            self.free.remove(&before);
            start = before;
        }
        if let Some(&len) = self.free.get(&end) {
            self.free.remove(&end);
            end += len;
        }
        self.free.insert(start, end - start);
        self.used -= bytes;
    }

    /// Whether nothing is given out.
    pub fn is_empty(&self) -> bool {
        self.used == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const K64: u64 = 64 << 10;

    #[test]
    fn first_fit_at_alignment_then_merged_back_whole() {
        let mut heap = HeapRanges::new(16 * K64);
        let a = heap.alloc(K64 + 1, K64).unwrap();
        let b = heap.alloc(3 * K64, K64).unwrap();
        let c = heap.alloc(100, 4096).unwrap();
        assert_eq!((a, b), (0, 2 * K64), "b starts at the next 64 KiB");
        assert_eq!(c, K64 + 4096, "the small one takes the gap after a");
        assert!(heap.alloc(16 * K64, K64).is_none());
        heap.release(b, 3 * K64);
        heap.release(a, K64 + 1);
        heap.release(c, 100);
        assert!(heap.is_empty());
        assert_eq!(
            heap.alloc(16 * K64, K64),
            Some(0),
            "all of it is one range again"
        );
    }

    #[test]
    fn released_ranges_are_reused_and_bounds_hold() {
        let mut heap = HeapRanges::new(8 * K64);
        let ranges: Vec<_> = (0..8).map(|_| heap.alloc(K64, K64).unwrap()).collect();
        assert!(heap.alloc(1, 1).is_none());
        heap.release(ranges[3], K64);
        heap.release(ranges[5], K64);
        assert!(
            heap.alloc(2 * K64, K64).is_none(),
            "two holes are not one range"
        );
        heap.release(ranges[4], K64);
        assert_eq!(heap.alloc(3 * K64, K64), Some(3 * K64));
        assert_eq!(heap.used(), 8 * K64);
        assert!(heap.alloc(0, K64).is_none());
        assert!(heap.alloc(1, 3).is_none());
    }

    #[test]
    fn every_byte_is_given_out_at_most_once() {
        let mut heap = HeapRanges::new(1 << 20);
        let mut owned = vec![false; 1 << 20];
        let mut live = Vec::new();
        for i in 0u64..600 {
            if i % 3 == 2 && !live.is_empty() {
                let (at, len): (u64, u64) = live.swap_remove((i as usize * 7) % live.len());
                owned[at as usize..(at + len) as usize].fill(false);
                heap.release(at, len);
                continue;
            }
            let len = 1 + (i * 2_654_435_761) % 20_000;
            let align = 1 << (i % 13);
            if let Some(at) = heap.alloc(len, align) {
                assert_eq!(at % align, 0);
                assert!(owned[at as usize..(at + len) as usize].iter().all(|o| !o));
                owned[at as usize..(at + len) as usize].fill(true);
                live.push((at, len));
            }
        }
        for (at, len) in live {
            heap.release(at, len);
        }
        assert!(heap.is_empty());
        assert_eq!(heap.alloc(1 << 20, 1), Some(0));
    }
}
