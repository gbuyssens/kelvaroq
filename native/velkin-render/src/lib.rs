//! Rendering logic shared by the native backend and hardware-independent tests.

pub mod compare_slots;
pub mod dirty_ranges;
pub mod heap_ranges;
pub mod pacing;
pub mod residency;
pub mod shadow_bytes;
#[cfg(windows)]
pub mod stats_ring;
pub mod texel_rows;
pub mod upload_pool;
