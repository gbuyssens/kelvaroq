use super::feed::{Feed, STAGES};
use crate::preload::visual::{self, t, tone};
use crate::preload::{color, rect};
use anyhow::Result;
use std::time::Duration;
use windows::{
    Win32::Graphics::{Direct2D::*, DirectWrite::*},
    core::w,
};
use windows_numerics::Vector2;

pub const W: u32 = 1200;
pub const H: u32 = 804;
const BG: u32 = 0x06131d;
const PANEL: u32 = 0x102430;
const MUTED: u32 = 0xa6bdc9;
const WHITE: u32 = 0xf1f6f7;
const MINT: u32 = 0xa1f4d4;
const GOLD: u32 = 0xf1bc77;
const BLUE: u32 = 0x75b5ec;

fn mib() -> &'static str {
    t("mib")
}
fn gib() -> &'static str {
    t("gib")
}
fn palette(c: u32) -> u32 {
    if !*visual::RESTYLE {
        return c;
    }
    match c {
        BG => tone(0xf7f9f7, 0x1b211e),
        PANEL => tone(0xffffff, 0x252c28),
        WHITE => tone(0x202925, 0xedf2ef),
        MUTED => tone(0x52635b, 0xb3c1b9),
        MINT => tone(0x17634c, 0x8ad7b5),
        GOLD => tone(0x965410, 0xf1bc77),
        BLUE => tone(0x32678c, 0x8fbddd),
        0x3a505c | 0x354a58 | 0x455864 => tone(0xcbd7cf, 0x48524b),
        other => other,
    }
}

pub struct Scene {
    pub focus: std::cell::Cell<Option<usize>>,
    target: ID2D1RenderTarget,
    brush: ID2D1SolidColorBrush,
    fonts: Vec<IDWriteTextFormat>,
}
fn num(n: f64, places: usize) -> String {
    let value = format!("{n:.places$}");
    if visual::english() {
        value
    } else {
        value.replace('.', ",")
    }
}
fn amount(v: Option<u64>, scale: f64, places: usize) -> String {
    v.map_or_else(|| "—".into(), |v| num(v as f64 / scale, places))
}
impl Scene {
    pub fn new(target: ID2D1RenderTarget) -> Result<Self> {
        unsafe {
            let brush = target.CreateSolidColorBrush(&color(WHITE, 1.), None)?;
            let write: IDWriteFactory = DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED)?;
            let mut fonts = Vec::new();
            for (size, weight) in [
                (14., DWRITE_FONT_WEIGHT_NORMAL),
                (16., DWRITE_FONT_WEIGHT_NORMAL),
                (20., DWRITE_FONT_WEIGHT_SEMI_BOLD),
                (38., DWRITE_FONT_WEIGHT_SEMI_BOLD),
                (15., DWRITE_FONT_WEIGHT_SEMI_BOLD),
            ] {
                let font = write.CreateTextFormat(
                    w!("Segoe UI"),
                    None,
                    weight,
                    DWRITE_FONT_STYLE_NORMAL,
                    DWRITE_FONT_STRETCH_NORMAL,
                    size,
                    w!("fr-FR"),
                )?;
                font.SetWordWrapping(DWRITE_WORD_WRAPPING_NO_WRAP)?;
                fonts.push(font);
            }
            Ok(Self {
                focus: Default::default(),
                target,
                brush,
                fonts,
            })
        }
    }
    fn fill(&self, x: f32, y: f32, w: f32, h: f32, r: u32) {
        if w <= 0. || h <= 0. {
            return;
        }
        unsafe {
            self.brush.SetColor(&color(palette(r), 1.));
            self.target.FillRectangle(&rect(x, y, w, h), &self.brush);
        }
    }
    fn label(&self, s: &str, x: f32, y: f32, w: f32, font: usize, c: u32) {
        unsafe {
            self.brush.SetColor(&color(palette(c), 1.));
            self.target.DrawText(
                &s.encode_utf16().collect::<Vec<_>>(),
                &self.fonts[font],
                &rect(x, y, w, if font == 3 { 55. } else { 32. }),
                &self.brush,
                D2D1_DRAW_TEXT_OPTIONS_CLIP,
                DWRITE_MEASURING_MODE_NATURAL,
            );
        }
    }
    fn line(&self, x: f32, y: f32, x2: f32, y2: f32, c: u32, width: f32) {
        unsafe {
            self.brush.SetColor(&color(palette(c), 1.));
            self.target.DrawLine(
                Vector2 { X: x, Y: y },
                Vector2 { X: x2, Y: y2 },
                &self.brush,
                width,
                None,
            );
        }
    }
    fn metric(&self, x: f32, title: &str, value: &str, unit: &str, c: u32) {
        self.fill(x, 83., 275., 98., PANEL);
        self.label(title, x + 16., 93., 245., 0, MUTED);
        self.label(value, x + 14., 112., 240., 3, c);
        self.label(unit, x + 16., 157., 245., 0, MUTED);
    }
    pub fn draw(&self, feed: &Feed, notice: &str) -> Result<()> {
        let h = feed.view();
        let stalled = feed.last.elapsed() > Duration::from_secs(1) || feed.pid.is_none();
        unsafe {
            self.target.BeginDraw();
            self.target.Clear(Some(&color(palette(BG), 1.)));
        }
        self.label("Velkin", 28., 17., 104., 2, MINT);
        self.fill(133., 21., 1., 21., 0x455864);
        self.label(t("monitor"), 149., 17., 445., 2, WHITE);
        self.label(&feed.status, 29., 48., 620., 0, MUTED);
        let has_frames = !h.rows.is_empty();
        self.fill(939., 17., 232., 44., if has_frames { MINT } else { PANEL });
        self.label(
            &t("saveCsv").replace("{0}", &h.seconds.to_string()),
            954.,
            28.,
            210.,
            4,
            if has_frames { BG } else { MUTED },
        );
        let ft = h.latest("frame_us");
        let fps = if has_frames && stalled && feed.inspection.is_none() {
            Some(0.)
        } else {
            h.mean("frame_us", 0).filter(|n| *n > 0.).map(|n| 1e6 / n)
        };
        self.metric(
            28.,
            t("rate"),
            &fps.map_or_else(|| "—".into(), |v| num(v, 1)),
            t("fpsUnit"),
            MINT,
        );
        self.metric(
            315.,
            t("lastFrame"),
            &amount(ft, 1000., 1),
            t("targetMs"),
            if ft.is_some_and(|n| n > 33333) {
                GOLD
            } else {
                WHITE
            },
        );
        let busy = h
            .latest("gpu_busy_us")
            .zip(ft)
            .filter(|(_, t)| *t > 0)
            .map(|(v, t)| format!("{} %", num(v as f64 * 100. / t as f64, 0)));
        self.metric(
            602.,
            t("gpuBusy"),
            &busy.unwrap_or_else(|| "—".into()),
            t("queueClock"),
            BLUE,
        );
        self.metric(
            889.,
            t("gpuIdle"),
            &amount(h.latest("gpu_idle_behind_us"), 1000., 1),
            t("lastMs"),
            WHITE,
        );

        self.fill(28., 194., 731., 342., PANEL);
        self.label(t("frameTimes"), 45., 205., 245., 2, WHITE);
        self.label(
            if feed.inspection.is_some() {
                t("live")
            } else {
                t("inspect")
            },
            496.,
            210.,
            244.,
            0,
            if feed.inspection.is_some() {
                MINT
            } else {
                MUTED
            },
        );
        let rows: Vec<_> = h.rows.iter().rev().take(120).rev().map(|v| &v.1).collect();
        let peak = rows
            .iter()
            .filter_map(|r| h.get(r, "frame_us"))
            .max()
            .unwrap_or(0);
        let top = ((peak.max(50_000) as f32 / 25_000.).ceil() * 25_000.).max(50_000.);
        let left = 102.;
        let right = 740.;
        let bottom = 335.;
        let high = 91.;
        let x = |i: usize| left + (120 - rows.len() + i) as f32 * (right - left) / 120.;
        let y = |us: u64| bottom - high * (us as f32 / top).min(1.);
        self.label(
            &format!("{} ms", (top / 1000.) as u64),
            44.,
            235.,
            58.,
            0,
            MUTED,
        );
        self.line(left, bottom, right, bottom, 0x3a505c, 1.);
        self.line(left, y(33333), right, y(33333), GOLD, 1.);
        self.label(&num(33.3, 1), 48., y(33333) - 10., 52., 0, GOLD);
        let mut previous = None;
        for (i, r) in rows.iter().enumerate() {
            let current = h.get(r, "frame_us").map(|us| (x(i), y(us)));
            if let (Some((px, py)), Some((cx, cy))) = (previous, current) {
                self.line(px, py, cx, cy, MINT, 2.);
            }
            previous = current;
        }
        self.label(t("activity"), 44., 348., 330., 2, WHITE);
        self.label(t("scale"), 450., 355., 292., 0, MUTED);
        for (name, base) in [(t("titleThread"), 413.), (t("submit"), 461.), ("GPU", 508.)] {
            self.label(name, 45., base - 27., 59., 1, WHITE);
            self.line(left, base, right, base, 0x354a58, 1.);
        }
        let col = (right - left) / 120. - 0.8;
        for (i, r) in rows.iter().enumerate() {
            for (base, name, c) in [(413., "title_us", 0xacc5e0), (508., "gpu_busy_us", BLUE)] {
                if let Some(n) = h.get(r, name) {
                    let size = 35. * (n as f32 / top).min(1.);
                    self.fill(x(i), base - size, col, size, c);
                }
            }
            let mut b = 461.;
            for &(key, _, c) in STAGES {
                if let Some(n) = h.get(r, key) {
                    let size = (35. * n as f32 / top).min((b - 426_f32).max(0.));
                    b -= size;
                    self.fill(x(i), b, col, size, c);
                }
            }
        }
        self.label(t("titleTime"), 45., 512., 690., 0, MUTED);
        self.fill(772., 194., 392., 342., PANEL);
        self.label(t("stages"), 790., 205., 361., 2, WHITE);
        self.label(t("avg"), 790., 237., 365., 0, MUTED);
        self.label(t("stage"), 790., 268., 170., 0, MUTED);
        self.label("ms", 1013., 268., 48., 0, MUTED);
        self.label(t("delta"), 1062., 268., 96., 0, MUTED);
        let mut ranked: Vec<_> = STAGES
            .iter()
            .filter_map(|&(key, title, c)| h.mean(key, 0).map(|v| (v, key, title, c)))
            .collect();
        ranked.sort_by(|a, b| b.0.total_cmp(&a.0));
        for (i, (v, key, title, c)) in ranked.iter().take(7).enumerate() {
            let y = 294. + i as f32 * 30.;
            self.fill(790., y + 6., 8., 8., *c);
            self.label(t(title), 807., y, 190., 1, WHITE);
            self.label(&num(v / 1000., 1), 1004., y, 68., 1, WHITE);
            let trend = h.mean(key, 1).map(|prev| {
                format!("{:+.1}", (v - prev) / 1000.)
                    .replace('.', if visual::english() { "." } else { "," })
            });
            self.label(
                &trend.unwrap_or_else(|| "—".into()),
                1080.,
                y,
                71.,
                1,
                MUTED,
            );
        }
        if rows.is_empty() {
            self.label(t("waitingFrame"), 790., 297., 352., 1, MUTED);
        }
        self.label(t("relayNote"), 790., 505., 355., 0, MUTED);
        let cols = [
            (28., 371., t("memory")),
            (
                411.,
                370.,
                if feed.show_drops {
                    t("drops")
                } else {
                    t("commands")
                },
            ),
            (793., 371., t("preparation")),
        ];
        for (x, w, title) in cols {
            self.fill(x, 550., w, 210., PANEL);
            self.label(title, x + 16., 562., w - 32., 2, WHITE);
        }
        let val = |k: &str, s: f64, p| amount(h.latest(k), s, p);
        let pair = |a: &str, b: &str, s: f64, p| format!("{} / {}", val(a, s, p), val(b, s, p));
        let memory = [
            (
                t("transfer"),
                format!(
                    "{} {}",
                    pair("to_card_bytes", "from_card_bytes", 1048576., 1),
                    mib()
                ),
            ),
            (
                t("inPlace"),
                format!("{} {}", val("inplace_bytes", 1048576., 1), mib()),
            ),
            (t("pages"), val("inplace_pages", 1., 0)),
            (
                t("vram"),
                format!(
                    "{} {}",
                    pair("vram_usage_bytes", "vram_budget_bytes", 1073741824., 1),
                    gib()
                ),
            ),
            (
                t("ram"),
                format!("{} {}", val("ram_bytes", 1073741824., 1), gib()),
            ),
        ];
        let drop_text = [
            ("drop_compiling", t("shortComp")),
            ("drop_unset", t("shortAbsent")),
            ("drop_refused", t("shortRefused")),
            ("drop_skipped", t("shortSkipped")),
            ("drop_signature", t("shortIndirect")),
            ("drop_builder", t("shortBuild")),
            ("drop_translation", t("shortTranslation")),
        ];
        let mut drops: Vec<_> = drop_text
            .iter()
            .filter_map(|(k, t)| h.latest(k).filter(|v| *v > 0).map(|v| (v, *t)))
            .collect();
        drops.sort_by(|a, b| b.0.cmp(&a.0));
        let reasons = drops
            .iter()
            .take(2)
            .map(|(v, t)| format!("{t} {v}"))
            .collect::<Vec<_>>()
            .join(" · ");
        let commands = [
            (t("draws"), pair("draws", "drawn", 1., 0)),
            (t("dispatch"), pair("dispatches", "dispatched", 1., 0)),
            (t("dropPair"), pair("drop_pipeline", "drop_indirect", 1., 0)),
            (
                t("mainReasons"),
                if reasons.is_empty() {
                    if h.latest("drop_compiling").is_some() {
                        t("none").into()
                    } else {
                        "—".into()
                    }
                } else {
                    reasons
                },
            ),
            (t("present"), format!("{} ms", val("present_us", 1000., 2))),
        ];
        let queues = [
            (t("building"), val("pipelines_making", 1., 0)),
            (
                t("pipelineWait"),
                format!("{} ms", val("pipeline_wait_us", 1000., 1)),
            ),
            (t("textures"), val("textures_waiting", 1., 0)),
            (
                t("uploads"),
                format!("{} {}", val("texture_upload_bytes", 1048576., 1), mib()),
            ),
            (t("queries"), val("watch_calls", 1., 0)),
        ];
        self.label(
            if feed.show_drops {
                t("back")
            } else {
                t("reasons")
            },
            663.,
            568.,
            115.,
            0,
            MINT,
        );
        for (x, items) in [(28., &memory), (411., &commands), (793., &queues)] {
            if x == 411. && feed.show_drops {
                for (i, (key, label)) in [
                    ("drop_compiling", t("dropComp")),
                    ("drop_unset", t("dropUnset")),
                    ("drop_refused", t("dropRefused")),
                    ("drop_skipped", t("dropSkip")),
                    ("drop_signature", t("dropIndirect")),
                    ("drop_builder", t("dropBuild")),
                    ("drop_translation", t("dropTranslate")),
                ]
                .iter()
                .enumerate()
                {
                    let y = 594. + i as f32 * 22.;
                    self.label(label, x + 16., y, 220., 0, MUTED);
                    self.label(&val(key, 1., 0), x + 246., y, 78., 0, WHITE);
                }
                continue;
            }
            for (i, (label, value)) in items.iter().enumerate() {
                let y = 599. + i as f32 * 29.;
                self.label(label, x + 16., y, 201., 0, MUTED);
                self.label(value, x + 211., y, 154., 0, WHITE);
            }
        }
        let text = if !notice.is_empty() {
            notice.into()
        } else if feed.pid.is_none() {
            t("waitingEngine").into()
        } else if feed.inspection.is_some() {
            t("selectedFrame").replace("{0}", &val("frame", 1., 0))
        } else if let Some(status) = h.latest("present_status").filter(|&v| v != 0) {
            t("noPresent").replace("{0}", &format!("0x{status:08x}"))
        } else if stalled {
            t("stalled").into()
        } else {
            t("intervals")
                .replace("{0}", &val("frame", 1., 0))
                .replace("{1}", &h.lost.to_string())
        };
        self.label(&text, 29., 774., 1140., 0, MUTED);
        if let Some(index) = self.focus.get() {
            let (x, y, w, h) = [
                (936., 14., 238., 50.),
                (100., 238., 642., 103.),
                (494., 200., 250., 37.),
                (410., 550., 372., 210.),
            ][index];
            self.line(x, y, x + w, y, WHITE, 2.);
            self.line(x, y + h, x + w, y + h, WHITE, 2.);
            self.line(x, y, x, y + h, WHITE, 2.);
            self.line(x + w, y, x + w, y + h, WHITE, 2.);
        }
        unsafe {
            self.target.EndDraw(None, None)?;
        }
        Ok(())
    }
}
