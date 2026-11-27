//! The reader follows the writer's column dictionary, including counters
//! introduced after this launcher was built. A lost lap leaves a sequence gap.

use crate::preload::visual::t;
use anyhow::{Context, Result, ensure};
use velkin_render::stats_ring::{ABSENT, CAPACITY, Reader, Writer};
use std::{
    collections::{HashMap, VecDeque},
    fs::File,
    io::{BufWriter, Write},
    path::Path,
    time::{Duration, Instant},
};

#[derive(Clone)]
pub struct History {
    pub names: Vec<String>,
    index: HashMap<String, usize>,
    pub rows: VecDeque<(u64, Vec<u64>)>,
    pub seconds: u64,
    pub lost: u64,
}
impl History {
    pub fn new(names: Vec<String>, seconds: u64) -> Self {
        let index = names
            .iter()
            .enumerate()
            .map(|(i, n)| (n.clone(), i))
            .collect();
        Self {
            names,
            index,
            rows: VecDeque::new(),
            seconds,
            lost: 0,
        }
    }
    pub fn get(&self, r: &[u64], name: &str) -> Option<u64> {
        r.get(*self.index.get(name)?)
            .copied()
            .filter(|&v| v != ABSENT)
    }
    pub fn latest(&self, name: &str) -> Option<u64> {
        self.get(&self.rows.back()?.1, name)
    }
    pub fn drain(&mut self, r: &Reader) {
        let newest = r.latest();
        let first = newest.saturating_sub(CAPACITY as u64 - 1).max(1);
        let next = self.rows.back().map_or(first, |s| s.0 + 1);
        for seq in next.max(first)..=newest {
            let Some(row) = r.read(seq) else { break };
            if let Some((last, _)) = self.rows.back() {
                self.lost += seq - last - 1;
            }
            self.rows.push_back((seq, row));
        }
        let cutoff = self
            .latest("timestamp_us")
            .unwrap_or(0)
            .saturating_sub(self.seconds.max(2) * 1_000_000);
        while self
            .rows
            .front()
            .is_some_and(|(_, r)| self.get(r, "timestamp_us").is_some_and(|t| t < cutoff))
        {
            self.rows.pop_front();
        }
    }
    pub fn mean(&self, key: &str, ago: u64) -> Option<f64> {
        let end = self.latest("timestamp_us")?.checked_sub(ago * 1_000_000)?;
        let mut n = 0;
        let mut sum = 0.;
        for (_, r) in self.rows.iter().rev() {
            let t = self.get(r, "timestamp_us")?;
            if t + 1_000_000 <= end {
                break;
            }
            if t <= end {
                if let Some(v) = self.get(r, key) {
                    sum += v as f64;
                    n += 1;
                }
            }
        }
        (n > 0).then(|| sum / n as f64)
    }
    pub fn csv(&self, file: &Path) -> Result<()> {
        ensure!(!self.rows.is_empty(), "{}", t("noFrames"));
        let mut w = BufWriter::new(File::options().write(true).create_new(true).open(file)?);
        writeln!(w, "sequence,{}", self.names.join(","))?;
        let cutoff = self
            .latest("timestamp_us")
            .unwrap_or(0)
            .saturating_sub(self.seconds * 1_000_000);
        for (seq, row) in &self.rows {
            if self.get(row, "timestamp_us").is_some_and(|t| t < cutoff) {
                continue;
            }
            write!(w, "{seq}")?;
            for v in row {
                if *v == ABSENT {
                    write!(w, ",")?
                } else {
                    write!(w, ",{v}")?
                }
            }
            writeln!(w)?;
        }
        w.flush()?;
        Ok(())
    }
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn K32EnumProcesses(pids: *mut u32, bytes: u32, used: *mut u32) -> i32;
}
fn processes() -> Vec<u32> {
    let mut list = vec![0; 1024];
    let mut used = 0;
    loop {
        let bytes = (list.len() * 4) as u32;
        if unsafe { K32EnumProcesses(list.as_mut_ptr(), bytes, &mut used) } == 0 {
            return Vec::new();
        }
        if used < bytes {
            list.truncate(used as usize / 4);
            return list;
        }
        list.resize(list.len() * 2, 0);
    }
}

pub struct Feed {
    pub history: History,
    pub inspection: Option<History>,
    pub show_drops: bool,
    pub pid: Option<u32>,
    pub status: String,
    pub last: Instant,
    reader: Option<Reader>,
    desired: Option<u32>,
    scan: Instant,
    pub demo: Option<Demo>,
}
impl Feed {
    pub fn new(seconds: u64, desired: Option<u32>, demo: bool) -> Result<Self> {
        let mut feed = Self {
            history: History::new(Vec::new(), seconds),
            inspection: None,
            show_drops: false,
            pid: None,
            status: t("searchEngine").into(),
            last: Instant::now(),
            reader: None,
            desired,
            scan: Instant::now() - Duration::from_secs(2),
            demo: None,
        };
        if demo {
            let mut source = Demo::new()?;
            for _ in 0..300 {
                source.step();
            }
            feed.demo = Some(source);
            feed.desired = Some(std::process::id());
        }
        feed.tick();
        Ok(feed)
    }
    pub fn tick(&mut self) {
        if let Some(d) = &mut self.demo {
            d.advance();
        }
        if self.scan.elapsed() >= Duration::from_secs(1) {
            self.scan = Instant::now();
            let pids = processes();
            if self.pid.is_some_and(|p| !pids.contains(&p)) {
                self.reader = None;
                self.pid = None;
                self.status = t("stopped").into();
            }
            if self.reader.is_none() {
                for pid in pids
                    .into_iter()
                    .filter(|p| self.desired.is_none_or(|want| want == *p))
                {
                    match Reader::open(pid) {
                        Ok(r)
                            if ["frame", "timestamp_us", "frame_us"]
                                .iter()
                                .all(|key| r.names.iter().any(|s| s == key)) =>
                        {
                            self.history = History::new(r.names.clone(), self.history.seconds);
                            self.inspection = None;
                            self.pid = Some(pid);
                            self.reader = Some(r);
                            self.status = if self.demo.is_some() {
                                t("synthetic").into()
                            } else {
                                t("connected").replace("{0}", &pid.to_string())
                            };
                            break;
                        }
                        Ok(_) => self.status = t("columns").into(),
                        Err(e) if e.kind() == std::io::ErrorKind::InvalidData => {
                            self.status = t("protocol").into()
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                            self.status = t("denied").into()
                        }
                        _ => {}
                    }
                }
            }
        }
        if let Some(r) = &self.reader {
            let before = self.history.rows.back().map(|v| v.0);
            self.history.drain(r);
            if self
                .history
                .rows
                .iter()
                .rev()
                .take_while(|(seq, _)| before.is_none_or(|old| *seq > old))
                .any(|(_, r)| self.history.get(r, "present_status").unwrap_or(0) == 0)
            {
                self.last = Instant::now();
            }
        }
    }
    pub fn view(&self) -> &History {
        self.inspection.as_ref().unwrap_or(&self.history)
    }
    pub fn select_column(&mut self, column: usize) {
        let h = self.view();
        let visible = h.rows.len().min(120);
        let Some(index) = column.checked_sub(120 - visible).filter(|&i| i < visible) else {
            return;
        };
        let mut copy = h.clone();
        copy.rows.truncate(h.rows.len() - visible + index + 1);
        self.inspection = Some(copy);
    }
    /// Hold a known synthetic frame while the bitmap is encoded. Rendering a
    /// preceding screenshot can take longer than the live stall threshold.
    pub fn demo_frame(&mut self, phase: u64) {
        let demo = self.demo.as_mut().expect("synthetic source");
        while demo.n % 400 != phase {
            demo.step();
        }
        self.history
            .drain(self.reader.as_ref().expect("synthetic reader"));
        self.last = Instant::now();
    }
    pub fn record(&self) -> Result<String> {
        use std::time::{SystemTime, UNIX_EPOCH};
        let stamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis();
        let name = format!("velkin-{}-{stamp}.csv", self.pid.unwrap_or(0));
        self.view().csv(Path::new(&name)).context(t("csvError"))?;
        Ok(name)
    }
}
pub const STAGES: &[(&str, &str, u32)] = &[
    ("descriptors_us", "desc", 0x63b5db),
    ("watch_us", "watch", 0xa1f4d4),
    ("deferred_us", "copies", 0xf1bc77),
    ("snapshots_us", "snaps", 0xc4a5ec),
    ("card_setup_us", "setup", 0x649ddc),
    ("execute_us", "lists", 0xe9879a),
    ("submit_other_us", "other", 0x8296aa),
    ("relay_wait_us", "relay", 0xcf8867),
    ("submit_lock_us", "locks", 0xe2aeb5),
    ("submit_card_us", "gpuWait", 0xaab66f),
    ("submit_fetch_us", "readWait", 0xb39980),
    ("submit_dependency_us", "deps", 0x8879aa),
    ("submit_idle_us", "idle", 0x334955),
];
const DEMO_NAMES: &[&str] = &[
    "frame",
    "timestamp_us",
    "frame_us",
    "title_us",
    "present_us",
    "gpu_busy_us",
    "gpu_idle_behind_us",
    "draws",
    "drawn",
    "dispatches",
    "dispatched",
    "drop_pipeline",
    "drop_indirect",
    "drop_compiling",
    "drop_refused",
    "pipelines_making",
    "pipeline_wait_us",
    "textures_waiting",
    "texture_upload_bytes",
    "to_card_bytes",
    "from_card_bytes",
    "inplace_pages",
    "inplace_bytes",
    "vram_budget_bytes",
    "vram_usage_bytes",
    "ram_bytes",
    "watch_calls",
    "watch_query_us",
];
pub struct Demo {
    writer: Writer,
    n: u64,
    t: u64,
    due: Instant,
}
impl Demo {
    fn new() -> Result<Self> {
        let names: Vec<_> = DEMO_NAMES
            .iter()
            .copied()
            .chain(STAGES.iter().map(|s| s.0))
            .collect();
        Ok(Self {
            writer: Writer::create(std::process::id(), &names)?,
            n: 0,
            t: 0,
            due: Instant::now(),
        })
    }
    pub fn step(&mut self) -> u64 {
        self.n += 1;
        let heavy = self.n % 400 >= 130;
        let n = self.n;
        let frame =
            (if heavy { 97_000 } else { 32_700 }) + (n * 137 % if heavy { 16_000 } else { 1250 });
        self.t += frame;
        let busy = frame * if heavy { 37 } else { 55 } / 100;
        let to_card = if heavy { 119 << 20 } else { 12 << 20 };
        let pages = if heavy { 8192 } else { 24576 };
        let drops = if heavy { n % 17 } else { 0 };
        let mut values = vec![
            n,
            self.t,
            frame,
            if heavy { 33800 } else { 32_000 },
            110,
            busy,
            (frame - busy) * 9 / 10,
            5331,
            5331 - drops,
            3100,
            3120,
            drops,
            0,
            drops,
            0,
            drops * 3,
            230,
            if heavy { 36 } else { 2 },
            2 << 20,
            to_card,
            to_card / 2,
            pages,
            pages << 16,
            15 << 30,
            (9 << 30) + n % 16 * (16 << 20),
            7 << 30,
            510,
            6900,
        ];
        let percentages = if heavy {
            [9, 41, 17, 4, 3, 1, 8, 15, 0, 1, 0, 0, 1]
        } else {
            [4, 8, 6, 1, 2, 1, 4, 3, 0, 1, 0, 0, 70]
        };
        values.extend(percentages.map(|pc| frame * pc / 100));
        self.writer.publish(&values);
        frame
    }
    fn advance(&mut self) {
        let now = Instant::now();
        while self.due <= now {
            let us = self.step();
            self.due += Duration::from_micros(us);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn missing_pid_is_an_empty_waiting_window() {
        let f = Feed::new(30, Some(u32::MAX), false).unwrap();
        assert!(f.pid.is_none() && f.history.rows.is_empty());
    }
    #[test]
    fn synthetic_writer_reaches_graph_and_csv() {
        let mut f = Feed::new(12, None, true).unwrap();
        assert_eq!(f.pid, Some(std::process::id()));
        assert!(f.history.mean("watch_us", 0).unwrap() > 30000.);
        let newest = f.history.latest("frame").unwrap();
        f.select_column(117);
        assert_eq!(f.view().latest("frame"), Some(newest - 2));
        f.inspection = None;
        assert_eq!(f.view().latest("frame"), Some(newest));
        let file = std::env::temp_dir().join(format!("frame-monitor-{}.csv", std::process::id()));
        f.history.csv(&file).unwrap();
        let s = std::fs::read_to_string(&file).unwrap();
        std::fs::remove_file(file).unwrap();
        assert!(s.lines().next().unwrap().contains("inplace_bytes"));
        assert!(s.lines().count() > 60);
    }
}
