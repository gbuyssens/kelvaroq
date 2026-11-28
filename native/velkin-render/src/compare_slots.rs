//! Ownership of GPU comparison results, in 32-bit words. The queue gives a
//! range back only after its fence and all consumers of its readback finish.

use crate::heap_ranges::HeapRanges;

pub struct CompareSlots {
    capacity: u32,
    tail: u32,
    free: Option<HeapRanges>,
}

impl CompareSlots {
    pub fn new(capacity: u32, recycle: bool) -> Self {
        Self {
            capacity,
            tail: 0,
            free: recycle.then(|| HeapRanges::new(u64::from(capacity))),
        }
    }

    pub fn reserve(&mut self, words: u32) -> Option<u32> {
        if words == 0 {
            return None;
        }
        if let Some(free) = &mut self.free {
            // Every offset is within the u32 capacity passed to new().
            return free.alloc(u64::from(words), 1).map(|at| at as u32);
        }
        let at = self.tail;
        let end = at.checked_add(words)?;
        if end > self.capacity {
            return None;
        }
        self.tail = end;
        Some(at)
    }

    pub fn release(&mut self, at: u32, words: u32) {
        if let Some(free) = &mut self.free {
            free.release(u64::from(at), u64::from(words));
        }
    }

    /// The original bump allocator rewinds only once all plans have landed.
    pub fn drained(&mut self) {
        debug_assert!(self.free.as_ref().is_none_or(|free| free.used() == 0));
        self.tail = 0;
    }

    pub fn more_than_half_used(&self) -> bool {
        let used = self
            .free
            .as_ref()
            .map_or(u64::from(self.tail), HeapRanges::used);
        used * 2 > u64::from(self.capacity)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retiring_middle_plan_preserves_live_results_and_reduces_pressure() {
        let mut slots = CompareSlots::new(12, true);
        assert_eq!(slots.reserve(3), Some(0));
        assert_eq!(slots.reserve(6), Some(3));
        assert_eq!(slots.reserve(3), Some(9));
        assert!(slots.more_than_half_used());
        slots.release(3, 6);
        assert!(!slots.more_than_half_used());
        assert_eq!(slots.reserve(7), None);
        assert_eq!(slots.reserve(6), Some(3));
        slots.release(9, 3);
        slots.release(3, 6);
        slots.release(0, 3);
        slots.drained();
        assert_eq!(slots.reserve(12), Some(0));
    }

    #[test]
    fn sequential_protocol_only_rewinds_after_all_consumers_finish() {
        let mut slots = CompareSlots::new(12, false);
        assert_eq!(slots.reserve(0), None);
        assert_eq!(slots.reserve(8), Some(0));
        slots.release(0, 8);
        assert!(slots.more_than_half_used());
        assert_eq!(slots.reserve(u32::MAX), None);
        assert_eq!(slots.reserve(4), Some(8));
        slots.release(8, 4);
        slots.drained();
        assert_eq!(slots.reserve(12), Some(0));
    }
}
