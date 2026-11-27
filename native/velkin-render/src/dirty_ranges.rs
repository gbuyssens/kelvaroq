//! Ranges changed since the last texture invalidation pass, with bounded storage.

use std::collections::BTreeMap;

pub struct DirtyRanges<const LIMIT: usize> {
    ends: BTreeMap<u64, u64>,
}

impl<const LIMIT: usize> DirtyRanges<LIMIT> {
    pub const fn new() -> Self {
        assert!(LIMIT > 0);
        Self {
            ends: BTreeMap::new(),
        }
    }

    /// Records [start, end). Returns true if the range had to cover a clean gap.
    pub fn insert(&mut self, mut start: u64, mut end: u64) -> bool {
        if start >= end {
            return false;
        }
        if let Some((&left, &left_end)) = self.ends.range(..=start).next_back()
            && left_end >= start
        {
            if left_end >= end {
                return false;
            }
            start = left;
            self.ends.remove(&left);
        }
        while let Some((&right, &right_end)) = self.ends.range(start..=end).next() {
            end = end.max(right_end);
            self.ends.remove(&right);
        }
        if self.ends.len() < LIMIT {
            self.ends.insert(start, end);
            return false;
        }
        // All buffered intervals are disjoint from this write. At capacity,
        // bridge only the smaller of its two neighbouring gaps. Every dirty
        // byte remains represented until the consumer takes the batch.
        let left = self
            .ends
            .range(..start)
            .next_back()
            .map(|(&s, &e)| (s, start - e));
        let right = self
            .ends
            .range(end..)
            .next()
            .map(|(&s, &e)| (s, e, s - end));
        match (left, right) {
            (Some((s, gap)), r) if r.is_none_or(|(_, _, other)| gap <= other) => {
                self.ends.insert(s, end);
            }
            (_, Some((s, e, _))) => {
                self.ends.remove(&s);
                self.ends.insert(start, e);
            }
            _ => unreachable!("a full nonempty map has a neighbour"),
        }
        true
    }

    pub fn into_ranges(self) -> impl ExactSizeIterator<Item = (u64, u64)> {
        self.ends.into_iter()
    }
}

impl<const LIMIT: usize> Default for DirtyRanges<LIMIT> {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn out_of_order_writes_bridge_both_neighbours_without_losing_holes() {
        let mut ranges = DirtyRanges::<4>::new();
        for (start, end) in [
            (10, 20),
            (40, 50),
            (25, 30),
            (20, 25),
            (29, 41),
            (0, 2),
            (10, 15),
        ] {
            assert!(!ranges.insert(start, end));
        }
        assert_eq!(ranges.into_ranges().collect::<Vec<_>>(), [(0, 2), (10, 50)]);
    }

    #[test]
    fn overflow_preserves_writes_on_either_side_and_across_batches() {
        let mut ranges = DirtyRanges::<2>::new();
        assert!(!ranges.insert(10, 15));
        assert!(!ranges.insert(30, 35));
        assert!(ranges.insert(25, 26));
        assert!(ranges.insert(4, 6));
        assert!(ranges.insert(38, 40));
        assert!(!ranges.insert(30, 30));
        let previous = std::mem::take(&mut ranges);
        assert_eq!(
            previous.into_ranges().collect::<Vec<_>>(),
            [(4, 15), (25, 40)]
        );
        assert!(!ranges.insert(u64::MAX - 1, u64::MAX));
        assert_eq!(ranges.into_ranges().len(), 1);
    }

    #[test]
    fn every_written_byte_survives_fragmentation() {
        let mut ranges = DirtyRanges::<7>::new();
        let mut written = [false; 509];
        for i in 0..130 {
            let start = i * 47 % 503;
            let end = start + (i % 5) + 1;
            ranges.insert(start as u64, end as u64);
            written[start..end].fill(true);
        }
        let result = ranges.into_ranges().collect::<Vec<_>>();
        assert!(result.len() <= 7);
        assert!(result.windows(2).all(|pair| pair[0].1 < pair[1].0));
        for (i, _) in written.into_iter().enumerate().filter(|(_, dirty)| *dirty) {
            assert!(result.iter().any(|&(a, b)| (a..b).contains(&(i as u64))));
        }
    }
}
