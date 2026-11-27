//! Reuse of mapped upload buffers, by size class and within a byte limit.
//!
//! A texture's staging buffer used to be created for each upload and
//! released after its list ran: one committed upload resource (pages the
//! system zeroes) per texture, hundreds per frame while a scene streams in.
//! Buffers are rounded up to a size class and given back here instead.

use std::collections::BTreeMap;

/// The smallest class: small buffers share one size.
const MIN: u64 = 64 << 10;

/// The size class of a buffer of `bytes`: quarter octaves from 64 KiB, so
/// a class is at most a quarter larger than what was asked for.
pub fn class(bytes: u64) -> u64 {
    let b = bytes.max(MIN);
    let top = 63 - b.leading_zeros();
    let step = 1u64 << top.saturating_sub(2);
    b.div_ceil(step).saturating_mul(step)
}

/// Buffers kept for reuse, `limit` bytes at most.
pub struct Pool<T> {
    free: BTreeMap<u64, Vec<T>>,
    bytes: u64,
    limit: u64,
}

impl<T> Pool<T> {
    pub const fn new(limit: u64) -> Self {
        Self {
            free: BTreeMap::new(),
            bytes: 0,
            limit,
        }
    }

    /// A kept buffer of the class of `bytes`, if there is one.
    pub fn take(&mut self, bytes: u64) -> Option<T> {
        let class = class(bytes);
        let list = self.free.get_mut(&class)?;
        let item = list.pop()?;
        if list.is_empty() {
            self.free.remove(&class);
        }
        self.bytes -= class;
        Some(item)
    }

    /// Keeps a buffer of `capacity` bytes (a class size) for reuse; gives it
    /// back when that would pass the limit or the size is not a class.
    pub fn give(&mut self, capacity: u64, item: T) -> Option<T> {
        if capacity != class(capacity) || self.bytes.saturating_add(capacity) > self.limit {
            return Some(item);
        }
        self.bytes += capacity;
        self.free.entry(capacity).or_default().push(item);
        None
    }

    /// Bytes kept.
    pub fn bytes(&self) -> u64 {
        self.bytes
    }

    /// Changes the limit, letting go of the largest buffers past it.
    pub fn set_limit(&mut self, limit: u64) -> Vec<T> {
        self.limit = limit;
        let mut out = Vec::new();
        while self.bytes > self.limit {
            let Some(mut entry) = self.free.last_entry() else {
                break;
            };
            let class = *entry.key();
            if let Some(item) = entry.get_mut().pop() {
                self.bytes -= class;
                out.push(item);
            }
            if entry.get().is_empty() {
                entry.remove();
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes_cover_the_request_with_at_most_a_quarter_more() {
        for bytes in [
            1,
            4096,
            MIN,
            MIN + 1,
            100_000,
            1 << 20,
            (1 << 20) + 1,
            3 << 20,
            22_369_621,
            1 << 30,
        ] {
            let c = class(bytes);
            assert!(c >= bytes.max(MIN), "{bytes}");
            assert!(c <= bytes.max(MIN) + bytes.max(MIN) / 4, "{bytes} -> {c}");
            assert_eq!(class(c), c, "a class is its own class");
        }
        assert_eq!(class(1), MIN);
        assert_eq!(class((1 << 20) + 1), (1 << 20) + (1 << 18));
    }

    #[test]
    fn reuse_is_by_class_and_bounded() {
        let mut pool = Pool::new(3 << 20);
        let a = class(1 << 20);
        assert!(pool.take(1 << 20).is_none());
        assert!(pool.give(a, "a").is_none());
        assert!(pool.give(a, "b").is_none());
        // Past the limit: handed back.
        assert_eq!(pool.give(class(2 << 20), "c"), Some("c"));
        // Not a class size: handed back.
        assert_eq!(pool.give(a + 1, "d"), Some("d"));
        assert_eq!(pool.bytes(), 2 << 20);
        // Another request of the same class takes one.
        assert!(pool.take(1_000_000).is_some());
        assert!(pool.take(1 << 19).is_none());
        assert!(pool.take(1 << 20).is_some());
        assert!(pool.take(1 << 20).is_none());
        assert_eq!(pool.bytes(), 0);
    }

    #[test]
    fn a_lower_limit_lets_the_largest_go() {
        let mut pool = Pool::new(u64::MAX);
        for (bytes, name) in [(MIN, "small"), (1 << 20, "mid"), (8 << 20, "big")] {
            assert!(pool.give(class(bytes), name).is_none());
        }
        assert_eq!(pool.set_limit(2 << 20), ["big"]);
        assert_eq!(pool.bytes(), MIN + (1 << 20));
    }
}
