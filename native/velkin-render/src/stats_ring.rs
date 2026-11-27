//! Version 1 of the local frame monitor protocol. All words are little-endian
//! aligned u64s. Names are ASCII, with zero padding. Units are part of the name.
//! The eight header words are magic, version, header bytes, slot bytes,
//! capacity, field count, PID and newest sequence. Then come 64 names of
//! 32 bytes each. Each slot holds a sequence and 64 values. Sequences start
//! at one; zero marks a slot being written. ABSENT marks a missing value.
//! The header carries the field order so a new counter needs no reader update.

use std::io;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU64, Ordering::*, fence};

pub const CAPACITY: usize = 512;
pub const FIELDS: usize = 64;
pub const ABSENT: u64 = u64::MAX;
const MAGIC: u64 = u64::from_le_bytes(*b"KELVSTAT");
const VERSION: u64 = 1;
const HEADER: usize = 8 + 4 * FIELDS;
const STRIDE: usize = 1 + FIELDS;
const WORDS: usize = HEADER + CAPACITY * STRIDE;
const BYTES: usize = WORDS * 8;

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateFileMappingW(
        file: isize,
        security: *const (),
        protect: u32,
        high: u32,
        low: u32,
        name: *const u16,
    ) -> isize;
    fn OpenFileMappingW(access: u32, inherit: i32, name: *const u16) -> isize;
    fn MapViewOfFile(
        handle: isize,
        access: u32,
        high: u32,
        low: u32,
        bytes: usize,
    ) -> *mut AtomicU64;
    fn UnmapViewOfFile(view: *const AtomicU64) -> i32;
    fn CloseHandle(handle: isize) -> i32;
    fn GetLastError() -> u32;
}

struct Mapping(NonNull<AtomicU64>, isize);
// SAFETY: the view stays mapped until drop. All shared accesses use aligned atomics.
unsafe impl Send for Mapping {}
unsafe impl Sync for Mapping {}
impl Mapping {
    fn open(pid: u32, create: bool) -> io::Result<Self> {
        let name: Vec<u16> = format!("Local\\VelkinStats-{pid}\0")
            .encode_utf16()
            .collect();
        // SAFETY: a terminated name, a paging-file mapping and a sized view.
        unsafe {
            let access = if create { 2 } else { 4 }; // FILE_MAP_WRITE / FILE_MAP_READ
            let handle = if create {
                CreateFileMappingW(-1, std::ptr::null(), 4, 0, BYTES as u32, name.as_ptr())
            } else {
                OpenFileMappingW(access, 0, name.as_ptr())
            };
            if handle == 0 {
                return Err(io::Error::last_os_error());
            }
            if create && GetLastError() == 183 {
                CloseHandle(handle);
                return Err(io::ErrorKind::AlreadyExists.into());
            }
            let ptr = NonNull::new(MapViewOfFile(handle, access, 0, 0, BYTES));
            let Some(ptr) = ptr else {
                let e = io::Error::last_os_error();
                CloseHandle(handle);
                return Err(e);
            };
            // Windows removes the name when the last mapping handle closes.
            // The writer owns that handle. A reader only needs its view.
            if create {
                Ok(Self(ptr, handle))
            } else {
                CloseHandle(handle);
                Ok(Self(ptr, 0))
            }
        }
    }

    fn words(&self) -> &[AtomicU64] {
        // SAFETY: MapViewOfFile verified the full length and returns page-aligned storage.
        unsafe { std::slice::from_raw_parts(self.0.as_ptr(), WORDS) }
    }
}
impl Drop for Mapping {
    fn drop(&mut self) {
        unsafe {
            UnmapViewOfFile(self.0.as_ptr());
            if self.1 != 0 {
                CloseHandle(self.1);
            }
        }
    }
}

pub struct Writer {
    map: Mapping,
    next: u64,
    count: usize,
}
impl Writer {
    pub fn create(pid: u32, names: &[&str]) -> io::Result<Self> {
        if names.is_empty()
            || names.len() > FIELDS
            || names.iter().enumerate().any(|(i, n)| {
                n.is_empty()
                    || n.len() > 31
                    || !n
                        .bytes()
                        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
                    || names[..i].contains(n)
            })
        {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        let map = Mapping::open(pid, true)?;
        let words = map.words();
        for (i, n) in [
            VERSION,
            HEADER as u64 * 8,
            STRIDE as u64 * 8,
            CAPACITY as u64,
            names.len() as u64,
            pid.into(),
        ]
        .into_iter()
        .enumerate()
        {
            words[1 + i].store(n, Relaxed);
        }
        for (i, n) in names.iter().enumerate() {
            let mut buf = [0; 32];
            buf[..n.len()].copy_from_slice(n.as_bytes());
            for (j, part) in buf.chunks_exact(8).enumerate() {
                words[8 + i * 4 + j].store(u64::from_le_bytes(part.try_into().unwrap()), Relaxed);
            }
        }
        words[0].store(MAGIC, Release);
        Ok(Self {
            map,
            next: 1,
            count: names.len(),
        })
    }

    /// Invalidate the previous lap before overwriting. The release/acquire
    /// fences pair when a reader observes any payload word from the new lap.
    pub fn publish(&mut self, values: &[u64]) {
        assert_eq!(self.count, values.len());
        let words = self.map.words();
        let start = slot(self.next);
        words[start].store(0, Relaxed);
        fence(Release);
        for (dst, src) in words[start + 1..].iter().zip(values) {
            dst.store(*src, Relaxed);
        }
        words[start].store(self.next, Release);
        words[7].store(self.next, Release);
        self.next += 1;
    }
}
fn slot(seq: u64) -> usize {
    HEADER + ((seq - 1) as usize % CAPACITY) * STRIDE
}
pub struct Reader {
    map: Mapping,
    pub names: Vec<String>,
}
impl Reader {
    pub fn open(pid: u32) -> io::Result<Self> {
        let map = Mapping::open(pid, false)?;
        let words = map.words();
        let word = |i: usize| words[i].load(Acquire);
        if word(0) != MAGIC {
            return Err(io::ErrorKind::InvalidData.into());
        }
        let count = word(5) as usize;
        if word(1) != VERSION
            || word(2) != HEADER as u64 * 8
            || word(3) != STRIDE as u64 * 8
            || word(4) != CAPACITY as u64
            || !(1..=FIELDS).contains(&count)
            || word(6) != u64::from(pid)
        {
            return Err(io::ErrorKind::InvalidData.into());
        }
        let mut names = Vec::new();
        for i in 0..count {
            let bytes: Vec<_> = (0..4)
                .flat_map(|j| word(8 + 4 * i + j).to_le_bytes())
                .take_while(|b| *b != 0)
                .collect();
            let name = String::from_utf8(bytes).map_err(|_| io::ErrorKind::InvalidData)?;
            if name.is_empty()
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
                || names.contains(&name)
            {
                return Err(io::ErrorKind::InvalidData.into());
            }
            names.push(name);
        }
        Ok(Self { map, names })
    }
    pub fn latest(&self) -> u64 {
        self.map.words()[7].load(Acquire)
    }
    pub fn read(&self, seq: u64) -> Option<Vec<u64>> {
        if seq == 0 {
            return None;
        }
        let words = self.map.words();
        let start = slot(seq);
        if words[start].load(Acquire) != seq {
            return None;
        }
        let values = words[start + 1..start + 1 + self.names.len()]
            .iter()
            .map(|v| v.load(Relaxed))
            .collect();
        fence(Acquire);
        (words[start].load(Relaxed) == seq).then_some(values)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reads_a_named_mapping_from_another_process() {
        if let Ok(pid) = std::env::var("VELKIN_RING_TEST_PARENT") {
            let reader = Reader::open(pid.parse().unwrap()).unwrap();
            assert_eq!(reader.names, ["frame", "new_page_counter"]);
            assert_eq!(reader.read(1).unwrap(), [73, 991]);
            return;
        }
        // A separate test may already publish under this process's normal ID.
        let key = std::process::id() | 0x8000_0000;
        let mut writer = Writer::create(key, &["frame", "new_page_counter"]).unwrap();
        writer.publish(&[73, 991]);
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "stats_ring::tests::reads_a_named_mapping_from_another_process",
            ])
            .env("VELKIN_RING_TEST_PARENT", key.to_string())
            .status()
            .unwrap();
        assert!(status.success());
    }

    #[test]
    fn consumer_checks_layout_and_never_accepts_a_torn_lap() {
        let pid = std::process::id();
        let mut w = Writer::create(pid, &["frame", "inverse"]).unwrap();
        let r = Reader::open(pid).unwrap();
        assert_eq!(r.names, ["frame", "inverse"]);
        assert!(Writer::create(pid, &["frame"]).is_err());
        w.map.words()[1].store(2, Release);
        assert!(Reader::open(pid).is_err());
        w.map.words()[1].store(VERSION, Release);
        let join = std::thread::spawn(move || {
            for n in 1..=50000 {
                w.publish(&[n, !n]);
            }
        });
        loop {
            let n = r.latest();
            if let Some(v) = r.read(n) {
                assert_eq!(v, [n, !n]);
            }
            if n == 50000 {
                break;
            }
        }
        join.join().unwrap();
        assert!(r.read(1).is_none());
        assert_eq!(r.read(50000).unwrap()[0], 50000);
        drop(r);
        assert!(Reader::open(pid).is_err());
    }
}
