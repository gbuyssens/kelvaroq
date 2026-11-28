#[path = "launcher/visual.rs"]
pub(crate) mod visual;

use anyhow::{Context, Result};
use std::time::{Duration, Instant};
use windows::{
    Win32::{
        Foundation::{HWND, LPARAM, LRESULT, WPARAM},
        Graphics::{
            Direct2D::{Common::*, *},
            DirectWrite::*,
            Dwm::{DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND, DwmSetWindowAttribute},
            Imaging::*,
        },
        System::{Com::*, LibraryLoader::GetModuleHandleW},
        UI::{Shell::SHCreateMemStream, WindowsAndMessaging::*},
    },
    core::{Error, HSTRING, Interface, w},
};

const WIDTH: i32 = 960;
const HEIGHT: i32 = 540;
const SKIP: u32 = WM_APP + 12;
const PAPER: u32 = 0xeff6fa;
const MINT: u32 = 0xa1f4d4;
const MUTED: u32 = 0xb1c4d3;

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Progress {
    pub done: u32,
    pub total: u32,
    pub failed: u32,
}

impl Progress {
    #[allow(dead_code)] // Only the companion CLI reads the engine pipe.
    pub fn parse(line: &str) -> Option<Self> {
        let mut parts = line.split_ascii_whitespace();
        if parts.next()? != "precompile" {
            return None;
        }
        let value = Self {
            done: parts.next()?.parse().ok()?,
            total: parts.next()?.parse().ok()?,
            failed: parts.next()?.parse().ok()?,
        };
        (parts.next().is_none() && value.done <= value.total && value.failed <= value.done)
            .then_some(value)
    }
}

fn grouped(n: u32) -> String {
    let mut s = String::new();
    for (i, c) in n.to_string().chars().rev().enumerate() {
        if i != 0 && i % 3 == 0 {
            s.push(' ');
        }
        s.push(c);
    }
    s.chars().rev().collect()
}

fn estimate(value: Progress, elapsed: Duration) -> String {
    if value.done == 0 {
        return "Estimation en cours…".into();
    }
    if value.done == value.total {
        return if value.failed > 0 {
            format!("{} en échec", grouped(value.failed))
        } else {
            "Terminé".into()
        };
    }
    let seconds = (elapsed.as_secs_f64() * f64::from(value.total - value.done)
        / f64::from(value.done))
    .ceil()
    .max(1.) as u64;
    if seconds < 60 {
        format!("Environ {seconds} s restantes")
    } else {
        format!("Environ {} min restantes", seconds.div_ceil(60))
    }
}

pub(crate) fn rect(x: f32, y: f32, w: f32, h: f32) -> D2D_RECT_F {
    D2D_RECT_F {
        left: x,
        top: y,
        right: x + w,
        bottom: y + h,
    }
}

pub(crate) fn color(rgb: u32, a: f32) -> D2D1_COLOR_F {
    D2D1_COLOR_F {
        r: ((rgb >> 16) & 255) as f32 / 255.,
        g: ((rgb >> 8) & 255) as f32 / 255.,
        b: (rgb & 255) as f32 / 255.,
        a,
    }
}

struct Apartment;
impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { CoUninitialize() }
    }
}

struct Form(HWND);
impl Drop for Form {
    fn drop(&mut self) {
        unsafe {
            let _ = DestroyWindow(self.0);
        }
    }
}

pub(crate) struct Window {
    form: Form,
    canvas: Canvas,
    _apartment: Apartment,
}

unsafe extern "system" fn events(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    // SAFETY: only this window's message thread receives these notifications.
    unsafe {
        match msg {
            WM_CLOSE => {
                let _ = PostMessageW(Some(hwnd), SKIP, WPARAM(0), LPARAM(0));
                LRESULT(0)
            }
            WM_ERASEBKGND => LRESULT(1),
            _ => DefWindowProcW(hwnd, msg, wp, lp),
        }
    }
}

impl Window {
    pub fn open() -> Result<Self> {
        visual::enable_dpi();
        // SAFETY: the form and drawing resources are owned by this STA thread.
        unsafe {
            CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok()?;
            let apartment = Apartment;
            let instance = GetModuleHandleW(None)?.into();
            let class = w!("VelkinShaderPreload");
            let wc = WNDCLASSW {
                lpfnWndProc: Some(events),
                hInstance: instance,
                hCursor: LoadCursorW(None, IDC_ARROW)?,
                lpszClassName: class,
                ..Default::default()
            };
            RegisterClassW(&wc);
            let dpi = if *visual::RESTYLE {
                windows::Win32::UI::HiDpi::GetDpiForSystem() as i32
            } else {
                96
            };
            let w = (WIDTH * dpi / 96).min(GetSystemMetrics(SM_CXSCREEN));
            let h = (HEIGHT * dpi / 96).min(GetSystemMetrics(SM_CYSCREEN));
            let form = Form(CreateWindowExW(
                WS_EX_APPWINDOW,
                class,
                w!("Velkin"),
                WS_POPUP,
                (GetSystemMetrics(SM_CXSCREEN) - w) / 2,
                (GetSystemMetrics(SM_CYSCREEN) - h) / 2,
                w,
                h,
                None,
                None,
                Some(instance),
                None,
            )?);
            let corners = DWMWCP_ROUND;
            let _ = DwmSetWindowAttribute(
                form.0,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                (&raw const corners).cast(),
                size_of_val(&DWMWCP_ROUND) as u32,
            );
            let canvas = Canvas::new(form.0)?;
            Ok(Self {
                form,
                canvas,
                _apartment: apartment,
            })
        }
    }

    pub fn run(&mut self, mut update: impl FnMut(bool) -> Result<(Progress, bool)>) -> Result<()> {
        let start = Instant::now();
        let mut frame = start;
        let mut fraction = 0.;
        let mut skip = false;
        let mut hover = false;
        let mut focused = false;
        let hwnd = self.form.0;
        // SAFETY: timer messages wake the UI even when the child is silent.
        unsafe {
            if SetTimer(Some(hwnd), 1, 16, None) == 0 {
                return Err(Error::from_thread().into());
            }
            let _ = ShowWindow(hwnd, SW_SHOW);
            loop {
                let mut msg = MSG::default();
                let status = GetMessageW(&mut msg, None, 0, 0).0;
                if status == -1 {
                    return Err(Error::from_thread().into());
                }
                if status == 0 {
                    return Ok(());
                }
                if msg.hwnd == hwnd {
                    let x = (msg.lParam.0 as i16) as i32;
                    let y = ((msg.lParam.0 >> 16) as i16) as i32;
                    let mut area = windows::Win32::Foundation::RECT::default();
                    GetClientRect(hwnd, &mut area)?;
                    let x = x * WIDTH / area.right.max(1);
                    let y = y * HEIGHT / area.bottom.max(1);
                    let on_button = (790..=906).contains(&x) && (448..=496).contains(&y);
                    match msg.message {
                        WM_MOUSEMOVE => hover = on_button,
                        WM_KEYDOWN if *visual::RESTYLE && msg.wParam.0 == 9 => focused = true,
                        WM_LBUTTONUP if on_button => skip = true,
                        WM_KEYDOWN if matches!(msg.wParam.0, 13 | 27 | 32) => skip = true,
                        SKIP => skip = true,
                        WM_TIMER => {
                            let (value, close) = update(skip)?;
                            let now = Instant::now();
                            let target = f64::from(value.done) / f64::from(value.total.max(1));
                            fraction += (target - fraction)
                                * (1. - (-14. * (now - frame).as_secs_f64()).exp());
                            if (fraction - target).abs() < 0.001 || value.done == value.total {
                                fraction = target;
                            }
                            frame = now;
                            self.canvas.draw(
                                value,
                                fraction as f32,
                                now - start,
                                hover || focused,
                                skip,
                            )?;
                            if close {
                                return Ok(());
                            }
                        }
                        _ => {}
                    }
                }
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
        }
    }
}

struct Canvas {
    target: ID2D1RenderTarget,
    brush: ID2D1SolidColorBrush,
    art: ID2D1Bitmap,
    mark: ID2D1Bitmap,
    brand: IDWriteTextFormat,
    heading: IDWriteTextFormat,
    number: IDWriteTextFormat,
    body: IDWriteTextFormat,
    right: IDWriteTextFormat,
    button: IDWriteTextFormat,
}

impl Canvas {
    fn new(hwnd: HWND) -> Result<Self> {
        unsafe {
            let factory: ID2D1Factory = D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, None)?;
            let mut size = windows::Win32::Foundation::RECT::default();
            GetClientRect(hwnd, &mut size)?;
            let target = factory.CreateHwndRenderTarget(
                &D2D1_RENDER_TARGET_PROPERTIES {
                    dpiX: 96. * size.right as f32 / WIDTH as f32,
                    dpiY: 96. * size.bottom as f32 / HEIGHT as f32,
                    ..Default::default()
                },
                &D2D1_HWND_RENDER_TARGET_PROPERTIES {
                    hwnd,
                    pixelSize: D2D_SIZE_U {
                        width: size.right as u32,
                        height: size.bottom as u32,
                    },
                    ..Default::default()
                },
            )?;
            Self::on(target.cast()?)
        }
    }

    fn on(target: ID2D1RenderTarget) -> Result<Self> {
        unsafe {
            let brush = target.CreateSolidColorBrush(&color(PAPER, 1.), None)?;
            let wic: IWICImagingFactory =
                CoCreateInstance(&CLSID_WICImagingFactory, None, CLSCTX_INPROC_SERVER)?;
            let bitmap = |bytes: &[u8]| -> Result<ID2D1Bitmap> {
                let stream = SHCreateMemStream(Some(bytes)).context("Lecture de l’illustration")?;
                let decoder = wic.CreateDecoderFromStream(
                    &stream,
                    std::ptr::null(),
                    WICDecodeMetadataCacheOnLoad,
                )?;
                let converter = wic.CreateFormatConverter()?;
                converter.Initialize(
                    &decoder.GetFrame(0)?,
                    &GUID_WICPixelFormat32bppPBGRA,
                    WICBitmapDitherTypeNone,
                    None,
                    0.,
                    WICBitmapPaletteTypeCustom,
                )?;
                Ok(target.CreateBitmapFromWicBitmap(&converter, None)?)
            };
            let art = bitmap(include_bytes!("../assets/launcher/fibres.png"))?;
            let mark = bitmap(include_bytes!("../assets/launcher/mark.png"))?;
            let write: IDWriteFactory = DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED)?;
            let font = |size, weight| {
                write.CreateTextFormat(
                    w!("Segoe UI"),
                    None,
                    weight,
                    DWRITE_FONT_STYLE_NORMAL,
                    DWRITE_FONT_STRETCH_NORMAL,
                    size,
                    w!("fr-FR"),
                )
            };
            let right = font(16., DWRITE_FONT_WEIGHT_NORMAL)?;
            right.SetTextAlignment(DWRITE_TEXT_ALIGNMENT_TRAILING)?;
            let button = font(16., DWRITE_FONT_WEIGHT_SEMI_BOLD)?;
            button.SetTextAlignment(DWRITE_TEXT_ALIGNMENT_CENTER)?;
            button.SetParagraphAlignment(DWRITE_PARAGRAPH_ALIGNMENT_CENTER)?;
            Ok(Self {
                target,
                brush,
                art,
                mark,
                right,
                button,
                brand: font(24., DWRITE_FONT_WEIGHT_SEMI_BOLD)?,
                heading: font(53., DWRITE_FONT_WEIGHT_SEMI_BOLD)?,
                number: font(32., DWRITE_FONT_WEIGHT_LIGHT)?,
                body: font(16., DWRITE_FONT_WEIGHT_NORMAL)?,
            })
        }
    }

    fn block(&self, r: D2D_RECT_F, radius: f32, rgb: u32, alpha: f32) {
        unsafe {
            self.brush.SetColor(&color(rgb, alpha));
            self.target.FillRoundedRectangle(
                &D2D1_ROUNDED_RECT {
                    rect: r,
                    radiusX: radius,
                    radiusY: radius,
                },
                &self.brush,
            );
        }
    }

    fn text(&self, text: &str, font: &IDWriteTextFormat, r: D2D_RECT_F, rgb: u32) {
        unsafe {
            self.brush.SetColor(&color(rgb, 1.));
            self.target.DrawText(
                &text.encode_utf16().collect::<Vec<_>>(),
                font,
                &r,
                &self.brush,
                D2D1_DRAW_TEXT_OPTIONS_CLIP,
                DWRITE_MEASURING_MODE_NATURAL,
            );
        }
    }

    fn draw_desktop(
        &self,
        value: Progress,
        fraction: f32,
        elapsed: Duration,
        hover: bool,
        skip: bool,
    ) -> Result<()> {
        use visual::{t, tone};
        let ink = tone(0x202925, 0xedf2ef);
        let muted = tone(0x52635b, 0xb3c1b9);
        let accent = tone(0x17634c, 0x8ad7b5);
        unsafe {
            self.target.BeginDraw();
            self.target
                .Clear(Some(&color(tone(0xf7f9f7, 0x1b211e), 1.)));
        }
        self.text("Velkin", &self.brand, rect(52., 42., 410., 44.), ink);
        self.text(
            t("warmTitle"),
            &self.heading,
            rect(50., 128., 870., 80.),
            ink,
        );
        self.text(t("warmHint"), &self.body, rect(54., 216., 840., 58.), muted);
        self.block(
            rect(32., 294., 896., 216.),
            6.,
            tone(0xffffff, 0x252c28),
            1.,
        );
        self.text(
            &format!("{:.0} %", fraction * 100.),
            &self.number,
            rect(55., 312., 132., 54.),
            accent,
        );
        let count = |n: u32| {
            if visual::english() {
                grouped(n).replace(' ', ",")
            } else {
                grouped(n)
            }
        };
        self.text(
            &t("pipelines")
                .replace("{0}", &count(value.done))
                .replace("{1}", &count(value.total)),
            &self.body,
            rect(185., 329., 395., 32.),
            ink,
        );
        let eta = if value.done == 0 {
            t("estimating").into()
        } else if value.total == value.done {
            if value.failed == 0 {
                t("done").into()
            } else {
                t("warmFailed").replace("{0}", &count(value.failed))
            }
        } else {
            let seconds = (elapsed.as_secs_f64() * f64::from(value.total - value.done)
                / f64::from(value.done))
            .ceil()
            .max(1.) as u64;
            if seconds < 60 {
                t("seconds").replace("{0}", &seconds.to_string())
            } else {
                t("minutes").replace("{0}", &seconds.div_ceil(60).to_string())
            }
        };
        self.text(&eta, &self.right, rect(580., 329., 324., 32.), muted);
        self.block(rect(55., 378., 850., 5.), 2., tone(0xd7dfda, 0x414b44), 1.);
        if fraction > 0. {
            self.block(rect(55., 378., 850. * fraction, 5.), 2., accent, 1.);
        }
        if skip {
            self.text(t("skipping"), &self.body, rect(55., 454., 680., 44.), muted);
        }
        self.block(
            rect(790., 448., 116., 48.),
            5.,
            if hover { accent } else { muted },
            1.,
        );
        self.block(
            rect(791., 449., 114., 46.),
            4.,
            tone(0xffffff, 0x252c28),
            1.,
        );
        self.text(t("skip"), &self.button, rect(790., 448., 116., 48.), ink);
        unsafe {
            self.target.EndDraw(None, None)?;
        }
        Ok(())
    }
    fn draw(
        &self,
        value: Progress,
        fraction: f32,
        elapsed: Duration,
        hover: bool,
        skip: bool,
    ) -> Result<()> {
        if *visual::RESTYLE {
            return self.draw_desktop(value, fraction, elapsed, hover, skip);
        }
        let t = elapsed.as_secs_f32();
        unsafe {
            self.target.BeginDraw();
            self.target.Clear(Some(&color(0x04101b, 1.)));
            self.target.DrawBitmap(
                &self.art,
                Some(&rect(0., 0., 960., 540.)),
                1.,
                D2D1_BITMAP_INTERPOLATION_MODE_LINEAR,
                None,
            );
            self.target.DrawBitmap(
                &self.mark,
                Some(&rect(53., 42., 32., 32.)),
                1.,
                D2D1_BITMAP_INTERPOLATION_MODE_LINEAR,
                None,
            );
        }
        for i in 0..18 {
            let x = 570. + ((i * 73) % 355) as f32;
            let y = 34. + ((i * 47) % 240) as f32 + (t * 0.32 + i as f32).sin() * 5.;
            self.block(
                rect(x, y, 2., 2.),
                1.,
                MINT,
                0.21 + 0.15 * (t + i as f32).sin(),
            );
        }
        self.text("Velkin", &self.brand, rect(98., 40., 255., 40.), PAPER);
        self.text(
            "Préchargement",
            &self.heading,
            rect(50., 125., 655., 74.),
            PAPER,
        );
        self.text(
            "des shaders",
            &self.heading,
            rect(50., 183., 545., 74.),
            PAPER,
        );
        self.block(rect(32., 296., 896., 215.), 18., 0x071422, 0.94);
        self.text(
            &format!("{:.0} %", fraction * 100.),
            &self.number,
            rect(55., 312., 135., 54.),
            MINT,
        );
        self.text(
            &format!(
                "{} / {} pipelines",
                grouped(value.done),
                grouped(value.total)
            ),
            &self.body,
            rect(185., 329., 380., 30.),
            PAPER,
        );
        self.text(
            &estimate(value, elapsed),
            &self.right,
            rect(585., 329., 320., 30.),
            MUTED,
        );
        self.block(rect(55., 376., 850., 6.), 3., 0x2d4051, 1.);
        if fraction > 0. {
            let width = 850. * fraction;
            self.block(rect(55., 374., width, 10.), 5., MINT, 0.09);
            self.block(rect(55., 376., width, 6.), 3., MINT, 1.);
        }
        self.block(rect(55., 413., 850., 1.), 0., 0x2a3b49, 0.85);
        self.text(
            if skip {
                "Enregistrement des pipelines terminés…"
            } else {
                "Cette préparation a lieu une fois après une mise à jour."
            },
            &self.body,
            rect(55., 458., 705., 35.),
            MUTED,
        );
        self.block(
            rect(790., 448., 116., 48.),
            24.,
            if hover { MINT } else { 0x77958f },
            1.,
        );
        self.block(
            rect(791., 449., 114., 46.),
            23.,
            if hover { 0x1d3a3d } else { 0x11252d },
            1.,
        );
        self.text("Passer", &self.button, rect(790., 448., 116., 48.), PAPER);
        unsafe {
            self.target.EndDraw(None, None)?;
        }
        Ok(())
    }
}

#[allow(dead_code)] // The desktop entry point owns the demo.
pub(crate) fn demo(fixed: Option<u32>) -> Result<()> {
    let mut window = Window::open()?;
    let start = Instant::now();
    window.run(|skip| {
        let seconds = start.elapsed().as_secs_f64();
        let fraction = fixed.map_or_else(
            || ((seconds - 1.5) / 14.).clamp(0., 1.),
            |p| f64::from(p) / 100.,
        );
        Ok((
            Progress {
                done: (fraction * 2752.).round() as u32,
                total: 2752,
                failed: 0,
            },
            skip || (fixed.is_none() && seconds >= 17.),
        ))
    })
}

/// The review demo presents the exact bitmap that WIC writes to the PNG.
/// Both render targets use the same drawing code, fonts and embedded art.
#[allow(dead_code)]
pub(crate) fn capture_demo(folder: &std::path::Path) -> Result<()> {
    std::fs::create_dir_all(folder)?;
    let window = Window::open()?;
    unsafe {
        let wic: IWICImagingFactory =
            CoCreateInstance(&CLSID_WICImagingFactory, None, CLSCTX_INPROC_SERVER)?;
        let bitmap = wic.CreateBitmap(
            960,
            540,
            &GUID_WICPixelFormat32bppPBGRA,
            WICBitmapCacheOnLoad,
        )?;
        let factory: ID2D1Factory = D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, None)?;
        let scene = Canvas::on(factory.CreateWicBitmapRenderTarget(
            &bitmap,
            &D2D1_RENDER_TARGET_PROPERTIES {
                dpiX: 96.,
                dpiY: 96.,
                ..Default::default()
            },
        )?)?;
        let _ = ShowWindow(window.form.0, SW_SHOW);
        for (pct, done, seconds) in [(0, 0, 0), (40, 1101, 32), (100, 2752, 80)] {
            scene.draw(
                Progress {
                    done,
                    total: 2752,
                    failed: 0,
                },
                pct as f32 / 100.,
                Duration::from_secs(seconds),
                false,
                false,
            )?;
            let display = &window.canvas.target;
            let frame = display.CreateBitmapFromWicBitmap(&bitmap, None)?;
            display.BeginDraw();
            display.DrawBitmap(
                &frame,
                None,
                1.,
                D2D1_BITMAP_INTERPOLATION_MODE_LINEAR,
                None,
            );
            display.EndDraw(None, None)?;
            save_png(
                &wic,
                &bitmap,
                &folder.join(format!("preload-{pct:03}.png")),
                960,
                540,
            )?;
        }
    }
    Ok(())
}

pub(crate) fn save_png(
    wic: &IWICImagingFactory,
    bitmap: &IWICBitmap,
    file: &std::path::Path,
    width: u32,
    height: u32,
) -> Result<()> {
    unsafe {
        let stream = wic.CreateStream()?;
        stream.InitializeFromFilename(
            &HSTRING::from(file.as_os_str()),
            windows::Win32::Foundation::GENERIC_WRITE.0,
        )?;
        let encoder = wic.CreateEncoder(&GUID_ContainerFormatPng, std::ptr::null())?;
        encoder.Initialize(&stream, WICBitmapEncoderNoCache)?;
        let mut encoded = None;
        encoder.CreateNewFrame(&mut encoded, std::ptr::null_mut())?;
        let encoded = encoded.context("Création de la capture")?;
        encoded.Initialize(None)?;
        encoded.SetSize(width, height)?;
        let mut format = GUID_WICPixelFormat32bppBGRA;
        encoded.SetPixelFormat(&mut format)?;
        encoded.WriteSource(bitmap, std::ptr::null())?;
        encoded.Commit()?;
        encoder.Commit()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn progress_protocol_and_french_labels() {
        let p = Progress::parse("precompile 1101 2752 0").unwrap();
        assert_eq!(grouped(p.total), "2 752");
        assert_eq!(
            estimate(p, Duration::from_secs(20)),
            "Environ 30 s restantes"
        );
        for bad in [
            "precompile 9 8 0",
            "precompile 1 2 2",
            "precompile 1 2",
            "log 1 2 0",
            "precompile 1 2 0 x",
        ] {
            assert!(Progress::parse(bad).is_none());
        }
    }
}
