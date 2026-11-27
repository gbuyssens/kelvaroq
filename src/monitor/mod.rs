//! Live frame diagnostics for the packaged launcher and its command-line companion.

mod feed;
mod scene;
use crate::preload::visual::t;
use anyhow::{Result, ensure};
use clap::Parser;
use feed::Feed;
use scene::{H, Scene, W};
use std::{
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use windows::core::HSTRING;
use windows::{
    Win32::{
        Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM},
        Graphics::{
            Direct2D::{Common::*, *},
            Imaging::*,
        },
        System::{Com::*, LibraryLoader::GetModuleHandleW},
        UI::WindowsAndMessaging::*,
    },
    core::{Error, Interface, w},
};

#[derive(Parser)]
struct Options {
    #[arg(long)]
    pid: Option<u32>,
    #[arg(long, default_value_t=30,value_parser=clap::value_parser!(u64).range(1..=600))]
    seconds: u64,
    #[arg(long)]
    capture: Option<PathBuf>,
}
pub(crate) fn command() -> Option<Result<()>> {
    let mut args = std::env::args_os().skip(1);
    let first = args.next()?;
    let demo = first == "--monitor-demo";
    if !demo && first != "--monitor" {
        return None;
    }
    Some((|| {
        let options = Options::try_parse_from(std::iter::once(first).chain(args))?;
        ensure!(
            demo || options.capture.is_none(),
            "--capture accompagne --monitor-demo"
        );
        ensure!(!demo || options.pid.is_none(), "--pid accompagne --monitor");
        unsafe {
            CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok()?;
        }
        let _com = Apartment;
        let feed = Feed::new(options.seconds, options.pid, demo)?;
        match options.capture {
            Some(dir) => capture(feed, &dir),
            None => window(feed),
        }
    })())
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
unsafe extern "system" fn events(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    unsafe {
        match msg {
            WM_CLOSE => {
                let _ = PostMessageW(Some(hwnd), WM_APP + 1, WPARAM(0), LPARAM(0));
                LRESULT(0)
            }
            WM_ERASEBKGND => LRESULT(1),
            _ => DefWindowProcW(hwnd, msg, wp, lp),
        }
    }
}
fn window(mut feed: Feed) -> Result<()> {
    crate::preload::visual::enable_dpi();
    unsafe {
        let class = w!("VelkinPerformanceMonitor");
        let instance = GetModuleHandleW(None)?.into();
        RegisterClassW(&WNDCLASSW {
            lpfnWndProc: Some(events),
            hInstance: instance,
            lpszClassName: class,
            hCursor: LoadCursorW(None, IDC_ARROW)?,
            ..Default::default()
        });
        let width = (GetSystemMetrics(SM_CXSCREEN) - 48).clamp(500, W as i32);
        let height = (GetSystemMetrics(SM_CYSCREEN) - 92).clamp(360, H as i32);
        let style = WS_OVERLAPPEDWINDOW;
        let mut r = RECT {
            right: width,
            bottom: height,
            ..Default::default()
        };
        AdjustWindowRect(&mut r, style, false)?;
        let form = Form(CreateWindowExW(
            WS_EX_APPWINDOW,
            class,
            &HSTRING::from(format!("Velkin · {}", t("monitor"))),
            style,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            r.right - r.left,
            r.bottom - r.top,
            None,
            None,
            Some(instance),
            None,
        )?);
        let hwnd = form.0;
        let factory: ID2D1Factory = D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, None)?;
        let target = factory.CreateHwndRenderTarget(
            &Default::default(),
            &D2D1_HWND_RENDER_TARGET_PROPERTIES {
                hwnd,
                pixelSize: D2D_SIZE_U {
                    width: width as u32,
                    height: height as u32,
                },
                ..Default::default()
            },
        )?;
        let scene = Scene::new(target.cast()?)?;
        let mut size = (0, 0);
        let mut notice = String::new();
        let mut notified = Instant::now();
        let mut column = 119usize;
        if SetTimer(Some(hwnd), 1, 50, None) == 0 {
            return Err(Error::from_thread().into());
        }
        let _ = ShowWindow(hwnd, SW_SHOW);
        loop {
            let mut msg = MSG::default();
            match GetMessageW(&mut msg, None, 0, 0).0 {
                -1 => return Err(Error::from_thread().into()),
                0 => return Ok(()),
                _ => {}
            }
            if msg.hwnd == hwnd {
                match msg.message {
                    WM_KEYDOWN if msg.wParam.0 == 27 && feed.inspection.is_some() => {
                        feed.inspection = None
                    }
                    m if m == WM_APP + 1 || (m == WM_KEYDOWN && msg.wParam.0 == 27) => {
                        return Ok(());
                    }
                    WM_KEYDOWN if *crate::preload::visual::RESTYLE && msg.wParam.0 == 9 => {
                        use windows::Win32::UI::Input::KeyboardAndMouse::{GetKeyState, VK_SHIFT};
                        let reverse = GetKeyState(VK_SHIFT.0 as i32) < 0;
                        let next = scene.focus.get().map_or(if reverse { 3 } else { 0 }, |i| {
                            (i + if reverse { 3 } else { 1 }) % 4
                        });
                        scene.focus.set(Some(next));
                    }
                    WM_KEYDOWN
                        if scene.focus.get() == Some(1) && matches!(msg.wParam.0, 37 | 39) =>
                    {
                        column = if msg.wParam.0 == 37 {
                            column.saturating_sub(1)
                        } else {
                            (column + 1).min(119)
                        };
                        feed.inspection = None;
                        feed.select_column(column);
                    }
                    WM_KEYDOWN
                        if matches!(msg.wParam.0, 13 | 32) && scene.focus.get().is_some() =>
                    {
                        match scene.focus.get() {
                            Some(0) if !feed.view().rows.is_empty() => {
                                notice = feed
                                    .record()
                                    .map(|s| t("csvSaved").replace("{0}", &s))
                                    .unwrap_or_else(|e| e.to_string());
                                notified = Instant::now();
                            }
                            Some(1) => feed.select_column(column),
                            Some(2) => feed.inspection = None,
                            Some(3) => feed.show_drops = !feed.show_drops,
                            _ => {}
                        }
                    }
                    WM_TIMER => {
                        feed.tick();
                        GetClientRect(hwnd, &mut r)?;
                        if r.right > 0 && r.bottom > 0 {
                            if size != (r.right, r.bottom) {
                                size = (r.right, r.bottom);
                                target.Resize(&D2D_SIZE_U {
                                    width: size.0 as u32,
                                    height: size.1 as u32,
                                })?;
                                target.SetDpi(
                                    96. * size.0 as f32 / W as f32,
                                    96. * size.1 as f32 / H as f32,
                                );
                            }
                            if notified.elapsed() > Duration::from_secs(8) {
                                notice.clear();
                            }
                            scene.draw(&feed, &notice)?;
                        }
                    }
                    WM_LBUTTONUP if size.0 > 0 && size.1 > 0 => {
                        scene.focus.set(None);
                        let x = (msg.lParam.0 as i16) as i32 * W as i32 / size.0;
                        let y = ((msg.lParam.0 >> 16) as i16) as i32 * H as i32 / size.1;
                        if (496..742).contains(&x) && (200..237).contains(&y) {
                            feed.inspection = None;
                        } else if (102..740).contains(&x) && (238..511).contains(&y) {
                            feed.select_column(((x - 102) * 120 / 638) as usize);
                        } else if (411..781).contains(&x) && (550..760).contains(&y) {
                            feed.show_drops = !feed.show_drops;
                        } else if (939..1171).contains(&x)
                            && (17..61).contains(&y)
                            && !feed.view().rows.is_empty()
                        {
                            notice = match feed.record() {
                                Ok(name) => t("csvSaved").replace("{0}", &name),
                                Err(e) => format!("{e:#}"),
                            };
                            notified = Instant::now();
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

fn capture(mut feed: Feed, dir: &Path) -> Result<()> {
    std::fs::create_dir_all(dir)?;
    unsafe {
        let wic: IWICImagingFactory =
            CoCreateInstance(&CLSID_WICImagingFactory, None, CLSCTX_INPROC_SERVER)?;
        let bitmap =
            wic.CreateBitmap(W, H, &GUID_WICPixelFormat32bppPBGRA, WICBitmapCacheOnLoad)?;
        let factory: ID2D1Factory = D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, None)?;
        let scene = Scene::new(factory.CreateWicBitmapRenderTarget(
            &bitmap,
            &D2D1_RENDER_TARGET_PROPERTIES {
                dpiX: 96.,
                dpiY: 96.,
                ..Default::default()
            },
        )?)?;
        let waiting = Feed::new(30, Some(u32::MAX), false)?;
        scene.draw(&waiting, "")?;
        crate::preload::save_png(&wic, &bitmap, &dir.join("monitor-attente.png"), W, H)?;
        ensure!(!feed.history.rows.is_empty(), "{}", t("demoFeed"));
        feed.demo_frame(196);
        scene.draw(&feed, "")?;
        crate::preload::save_png(&wic, &bitmap, &dir.join("monitor-charge.png"), W, H)?;
        feed.demo_frame(119);
        scene.draw(&feed, "")?;
        crate::preload::save_png(&wic, &bitmap, &dir.join("monitor-cadence.png"), W, H)?;
    }
    Ok(())
}
