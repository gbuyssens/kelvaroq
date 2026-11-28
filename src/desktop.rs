use crate::image::Image;
use anyhow::{Context, Result, anyhow};
use std::{
    io::{BufRead, BufReader, BufWriter, Read, Write},
    os::windows::process::CommandExt,
    process::{Child, Command, ExitCode, Stdio},
    sync::mpsc::{self, Receiver, RecvTimeoutError, Sender},
    thread,
    time::Duration,
};
use windows::{
    Win32::{
        Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, RECT, WPARAM},
        Graphics::Gdi::{
            BLACK_BRUSH, COLOR_WINDOW, DEFAULT_GUI_FONT, GetStockObject, GetSysColorBrush, HBRUSH,
        },
        System::{LibraryLoader::GetModuleHandleW, Threading::CREATE_NO_WINDOW},
        UI::{
            Controls::{EM_GETSEL, EM_REPLACESEL, EM_SCROLLCARET, EM_SETLIMITTEXT, EM_SETSEL},
            WindowsAndMessaging::*,
        },
    },
    core::{Error, PCWSTR, w},
};

const EDIT_ID: i32 = 101;
const VIEWPORT_ID: i32 = 102;
const LOG_TIMER: usize = 1;

/// Render and journal windows belong to this thread. The worker owns all process I/O.
pub fn run(command: Command, plan: &Image) -> Result<ExitCode> {
    let windows = Desktop::open()?;
    let (send, events) = mpsc::channel();
    let (cancel, closed) = mpsc::channel();
    thread::scope(|scope| {
        let worker = scope.spawn(move || {
            let status = match supervise(command, plan, &send, closed) {
                Ok(code) => code,
                Err(error) => {
                    let _ = send.send(Event::Log(format!("Erreur du moteur : {error:#}")));
                    ExitCode::FAILURE
                }
            };
            let _ = send.send(Event::Finished);
            status
        });
        let ui = windows.pump(events);
        drop(cancel);
        let status = worker
            .join()
            .map_err(|_| anyhow!("worker supervisor panicked"))?;
        ui?;
        Ok(status)
    })
}

enum Event {
    Log(String),
    Finished,
}

// Drop runs inside the I/O thread scope, before its pipe readers are joined.
struct Worker(Child);

impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn supervise(
    mut command: Command,
    plan: &Image,
    events: &Sender<Event>,
    closed: Receiver<()>,
) -> Result<ExitCode> {
    let mut process = command
        .creation_flags(CREATE_NO_WINDOW.0)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("starting the reference CPU")?;
    let mut stdin = BufWriter::new(process.stdin.take().expect("piped stdin"));
    let mut stdout = process.stdout.take().expect("piped stdout");
    let stderr = process.stderr.take().expect("piped stderr");
    thread::scope(|scope| {
        let mut child = Worker(process);
        let writer = scope.spawn(move || -> Result<()> {
            serde_json::to_writer(&mut stdin, plan)?;
            stdin.flush()?;
            Ok(())
        });
        let report = scope.spawn(move || -> std::io::Result<String> {
            let mut text = String::new();
            stdout.read_to_string(&mut text)?;
            Ok(text)
        });
        let logs = scope.spawn(|| -> std::io::Result<()> {
            for line in BufReader::new(stderr).lines() {
                if events.send(Event::Log(line?)).is_err() {
                    break;
                }
            }
            Ok(())
        });
        let status = loop {
            if let Some(status) = child.0.try_wait()? {
                break status;
            }
            if !matches!(
                closed.recv_timeout(Duration::from_millis(50)),
                Err(RecvTimeoutError::Timeout)
            ) {
                return Ok(ExitCode::SUCCESS);
            }
        };
        // Publish stderr first so the final report stays at the bottom of the journal.
        logs.join()
            .map_err(|_| anyhow!("stderr reader panicked"))??;
        let text = report
            .join()
            .map_err(|_| anyhow!("stdout reader panicked"))??;
        let mut final_log = format!("Fin du moteur ({status}).");
        if !text.trim().is_empty() {
            final_log.push('\n');
            final_log.push_str(&report_summary(&text, plan.callbacks.len()));
        }
        let _ = events.send(Event::Log(final_log));
        writer
            .join()
            .map_err(|_| anyhow!("stdin writer panicked"))??;
        Ok(status
            .code()
            .and_then(|code| u8::try_from(code).ok())
            .map_or(ExitCode::FAILURE, ExitCode::from))
    })
}

#[derive(serde::Deserialize)]
struct Report {
    console: String,
    completed: Vec<Stage>,
    calls: std::collections::BTreeMap<String, u64>,
    initializers_entered: Option<u64>,
    stop: Option<Stop>,
    result: Option<u64>,
}

#[derive(serde::Deserialize)]
struct Stage {
    stage: String,
}

#[derive(serde::Deserialize)]
struct Stop {
    reason: String,
    detail: Option<String>,
    instruction: Option<String>,
    dll: Option<String>,
    symbol: Option<String>,
    return_rva: Option<String>,
    pc_rva: Option<String>,
}

fn report_summary(text: &str, expected_tls: usize) -> String {
    let Ok(report) = serde_json::from_str::<Report>(text) else {
        return text.trim_end().to_owned();
    };
    let tls = report
        .completed
        .iter()
        .filter(|stage| stage.stage == "tls-attach")
        .count();
    let calls = report.calls.values().copied().sum::<u64>();
    let mut summary = format!(
        "Profil {} · TLS terminés {tls}/{expected_tls} · appels {calls}",
        report.console
    );
    if let Some(count) = report.initializers_entered {
        summary.push_str(&format!(" · initialiseurs atteints {count}"));
    }
    if let Some(stop) = report.stop {
        let mut parts = vec![stop.reason];
        if let Some(detail) = stop.detail {
            parts.push(detail);
        }
        if let Some(instruction) = stop.instruction {
            parts.push(instruction);
        }
        if let Some(symbol) = stop.symbol {
            parts.push(
                stop.dll
                    .map_or_else(|| symbol.clone(), |dll| format!("{dll}!{symbol}")),
            );
        }
        if let Some(rva) = stop.return_rva {
            parts.push(format!("RVA de retour {rva}"));
        }
        if let Some(rva) = stop.pc_rva {
            parts.push(format!("RVA d’instruction {rva}"));
        }
        summary.push_str(&format!("\nArrêt · {}", parts.join(" · ")));
    } else if let Some(result) = report.result {
        summary.push_str(&format!("\nRetour du programme · {result:#x}"));
    }
    summary
}

struct Window(HWND);

impl Drop for Window {
    fn drop(&mut self) {
        // SAFETY: top-level windows are destroyed by their owning UI thread.
        let _ = unsafe { DestroyWindow(self.0) };
    }
}

struct Desktop {
    // Release the DXGI association before destroying its HWND.
    _surface: Option<crate::gpu::DisplaySurface>,
    render: Window,
    _journal: Window,
    edit: HWND,
}

impl Desktop {
    fn open() -> Result<Self> {
        // SAFETY: all windows and controls are created and used on this thread. The
        // callbacks retain no references. System brushes and the font are stock objects.
        unsafe {
            let module: HINSTANCE = GetModuleHandleW(None)?.into();
            let cursor = LoadCursorW(None, IDC_ARROW)?;
            let render_class = w!("VelkinRender");
            let log_class = w!("VelkinJournal");
            let classes: [(PCWSTR, WNDPROC, HBRUSH); 2] = [
                (
                    render_class,
                    Some(render_proc),
                    HBRUSH(GetStockObject(BLACK_BRUSH).0),
                ),
                (
                    log_class,
                    Some(journal_proc),
                    GetSysColorBrush(COLOR_WINDOW),
                ),
            ];
            for (class, proc, background) in classes {
                let cls = WNDCLASSW {
                    lpfnWndProc: proc,
                    hInstance: module,
                    hCursor: cursor,
                    hbrBackground: background,
                    lpszClassName: class,
                    ..Default::default()
                };
                if RegisterClassW(&cls) == 0 {
                    return Err(Error::from_thread().into());
                }
            }
            let mut area = RECT::default();
            SystemParametersInfoW(
                SPI_GETWORKAREA,
                0,
                Some((&raw mut area).cast()),
                Default::default(),
            )?;
            let total = (area.right - area.left - 16).clamp(200, 1600);
            let render_width = total * 3 / 5;
            let height = (area.bottom - area.top - 32).clamp(180, 720);
            let left = area.left + (area.right - area.left - total) / 2;
            let top = area.top + (area.bottom - area.top - height) / 2;
            let render = Window(CreateWindowExW(
                Default::default(),
                render_class,
                w!("Velkin — Rendu"),
                WS_OVERLAPPEDWINDOW,
                left,
                top,
                render_width,
                height,
                None,
                None,
                Some(module),
                None,
            )?);
            let viewport = CreateWindowExW(
                Default::default(),
                render_class,
                None,
                WS_CHILD | WS_VISIBLE,
                0,
                0,
                1,
                1,
                Some(render.0),
                Some(HMENU(VIEWPORT_ID as usize as *mut _)),
                Some(module),
                None,
            )?;
            fit_viewport(render.0, viewport)?;
            let journal = Window(CreateWindowExW(
                Default::default(),
                log_class,
                w!("Velkin — Journal"),
                WS_OVERLAPPEDWINDOW,
                left + render_width,
                top,
                total - render_width,
                height,
                None,
                None,
                Some(module),
                None,
            )?);
            let mut client = RECT::default();
            GetClientRect(journal.0, &mut client)?;
            let edit = CreateWindowExW(
                WS_EX_CLIENTEDGE,
                w!("EDIT"),
                None,
                WS_CHILD
                    | WS_VISIBLE
                    | WS_VSCROLL
                    | WS_TABSTOP
                    | WINDOW_STYLE((ES_MULTILINE | ES_AUTOVSCROLL | ES_READONLY) as u32),
                0,
                0,
                client.right,
                client.bottom,
                Some(journal.0),
                Some(HMENU(EDIT_ID as usize as *mut _)),
                Some(module),
                None,
            )?;
            SendMessageW(
                edit,
                WM_SETFONT,
                Some(WPARAM(GetStockObject(DEFAULT_GUI_FONT).0 as usize)),
                Some(LPARAM(1)),
            );
            SendMessageW(edit, EM_SETLIMITTEXT, Some(WPARAM(0x7fff_fffe)), None);
            append(
                edit,
                "Démarrage du moteur. En attente de la première image.\n",
            );
            if SetTimer(Some(render.0), LOG_TIMER, 50, None) == 0 {
                return Err(Error::from_thread().into());
            }
            let surface = match crate::gpu::DisplaySurface::open(viewport) {
                Ok(surface) => {
                    append(
                        edit,
                        &format!(
                            "Surface Direct3D 12 sur {}. Deux tampons vérifiés à {} × {}.\n",
                            surface.adapter_name,
                            crate::gpu::OUTPUT_WIDTH,
                            crate::gpu::OUTPUT_HEIGHT
                        ),
                    );
                    Some(surface)
                }
                Err(error) => {
                    append(
                        edit,
                        &format!("Surface Direct3D 12 indisponible : {error:#}\n"),
                    );
                    None
                }
            };
            let _ = ShowWindow(journal.0, SW_SHOWNOACTIVATE);
            let _ = ShowWindow(render.0, SW_SHOW);
            Ok(Self {
                _surface: surface,
                render,
                _journal: journal,
                edit,
            })
        }
    }

    fn pump(&self, events: Receiver<Event>) -> Result<()> {
        // SAFETY: GetMessageW initializes MSG. The handle and edit remain live
        // until this loop ends. WM_CLOSE on the render window posts WM_QUIT.
        unsafe {
            let mut message = MSG::default();
            loop {
                match GetMessageW(&mut message, None, 0, 0).0 {
                    -1 => return Err(Error::from_thread().into()),
                    0 => return Ok(()),
                    _ => {}
                }
                if message.hwnd == self.render.0
                    && message.message == WM_TIMER
                    && message.wParam.0 == LOG_TIMER
                {
                    let mut lines = String::new();
                    for event in events.try_iter().take(256) {
                        match event {
                            Event::Log(text) => {
                                lines.push_str(&text);
                                lines.push('\n');
                            }
                            Event::Finished => {
                                KillTimer(Some(self.render.0), LOG_TIMER)?;
                                SetWindowTextW(self.render.0, w!("Velkin — Émulation terminée"))?;
                            }
                        }
                    }
                    if !lines.is_empty() {
                        append(self.edit, &lines);
                    }
                } else {
                    let _ = TranslateMessage(&message);
                    DispatchMessageW(&message);
                }
            }
        }
    }
}

fn append(edit: HWND, text: &str) {
    let text: Vec<u16> = text
        .replace("\r\n", "\n")
        .replace('\n', "\r\n")
        .replace('\0', "�")
        .encode_utf16()
        .chain([0])
        .collect();
    // SAFETY: edit is a native Unicode EDIT control on this thread. SendMessageW
    // is synchronous, so all stack and text pointers outlive their use.
    unsafe {
        let end = GetWindowTextLengthW(edit) as u32;
        let (mut first, mut last) = (0u32, 0u32);
        SendMessageW(
            edit,
            EM_GETSEL,
            Some(WPARAM((&raw mut first) as usize)),
            Some(LPARAM((&raw mut last) as isize)),
        );
        SendMessageW(
            edit,
            EM_SETSEL,
            Some(WPARAM(end as usize)),
            Some(LPARAM(end as isize)),
        );
        SendMessageW(
            edit,
            EM_REPLACESEL,
            None,
            Some(LPARAM(PCWSTR(text.as_ptr()).0 as isize)),
        );
        if first == end && last == end {
            SendMessageW(edit, EM_SCROLLCARET, None, None);
        } else {
            SendMessageW(
                edit,
                EM_SETSEL,
                Some(WPARAM(first as usize)),
                Some(LPARAM(last as isize)),
            );
        }
    }
}

fn fit_viewport(parent: HWND, viewport: HWND) -> windows::core::Result<()> {
    // SAFETY: the viewport is a child of parent on this UI thread.
    unsafe {
        let mut area = RECT::default();
        GetClientRect(parent, &mut area)?;
        let w = area.right.min(area.bottom * 16 / 9);
        let h = area.bottom.min(area.right * 9 / 16);
        MoveWindow(
            viewport,
            (area.right - w) / 2,
            (area.bottom - h) / 2,
            w,
            h,
            true,
        )
    }
}

unsafe extern "system" fn render_proc(window: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    // SAFETY: Win32 calls this function with the current window and message parameters.
    unsafe {
        if msg == WM_CLOSE {
            PostQuitMessage(0);
            return LRESULT(0);
        }
        if msg == WM_SIZE
            && let Ok(viewport) = GetDlgItem(Some(window), VIEWPORT_ID)
        {
            let _ = fit_viewport(window, viewport);
        }
        DefWindowProcW(window, msg, wp, lp)
    }
}

unsafe extern "system" fn journal_proc(window: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    // SAFETY: the optional child is looked up each time and sized in client coordinates.
    unsafe {
        match msg {
            WM_SIZE => {
                let mut client = RECT::default();
                if let Ok(edit) = GetDlgItem(Some(window), EDIT_ID)
                    && GetClientRect(window, &mut client).is_ok()
                {
                    let _ = MoveWindow(edit, 0, 0, client.right, client.bottom, true);
                }
                LRESULT(0)
            }
            WM_CLOSE => {
                let _ = ShowWindow(window, SW_MINIMIZE);
                LRESULT(0)
            }
            _ => DefWindowProcW(window, msg, wp, lp),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn worker(script: &str) -> Command {
        let mut command = Command::new("powershell.exe");
        command.args(["-NoProfile", "-NonInteractive", "-Command", script]);
        command
    }

    #[test]
    fn stop_summary_keeps_the_call_site_on_the_last_line() {
        let report = r#"{
            "console":"series-x","completed":[{"stage":"tls-attach"}],
            "calls":{"a":5,"b":8},"initializers_entered":101,
            "stop":{"reason":"unimplemented import","dll":"example.dll","symbol":"Example","return_rva":"0x1234"},
            "virtual_reservations":[{"unused":"large payload"}]
        }"#;
        let summary = report_summary(report, 2);
        assert!(summary.contains("TLS terminés 1/2 · appels 13 · initialiseurs atteints 101"));
        assert_eq!(
            summary.lines().last(),
            Some("Arrêt · unimplemented import · example.dll!Example · RVA de retour 0x1234")
        );
        assert_eq!(summary.lines().count(), 2);
    }

    #[test]
    fn summary_handles_returns_and_plain_text_errors() {
        let report = r#"{"console":"series-s","completed":[],"calls":{"example":2},"result":42}"#;
        assert_eq!(
            report_summary(report, 0),
            "Profil series-s · TLS terminés 0/0 · appels 2\nRetour du programme · 0x2a"
        );
        assert_eq!(
            report_summary("RuntimeError: example\n", 0),
            "RuntimeError: example"
        );
    }

    #[test]
    fn report_keeps_stderr_and_worker_status() -> Result<()> {
        let plan = crate::image::inspect(&std::env::current_exe()?)?;
        let command = worker(
            r#"
            $plan = [Console]::In.ReadToEnd() | ConvertFrom-Json
            [Console]::Error.WriteLine('progress')
            [Console]::Out.WriteLine((@{ sha256 = $plan.sha256 } | ConvertTo-Json -Compress))
            exit 2
        "#,
        );
        let (send, events) = mpsc::channel();
        let (_cancel, closed) = mpsc::channel();
        let status = supervise(command, &plan, &send, closed)?;
        assert_eq!(status, ExitCode::from(2));
        assert!(matches!(events.try_recv(), Ok(Event::Log(text)) if text == "progress"));
        assert!(matches!(events.try_recv(), Ok(Event::Log(text)) if text.contains(&plan.sha256)));
        Ok(())
    }

    #[test]
    fn closing_view_reaps_live_worker() -> Result<()> {
        let plan = crate::image::inspect(&std::env::current_exe()?)?;
        let command = worker("[Console]::Error.WriteLine($PID); Start-Sleep -Seconds 8; exit 2");
        let (send, events) = mpsc::channel();
        let (cancel, closed) = mpsc::channel();
        thread::scope(|scope| -> Result<()> {
            let run = scope.spawn(|| supervise(command, &plan, &send, closed));
            let Event::Log(text) = events.recv_timeout(Duration::from_secs(10))? else {
                anyhow::bail!("missing worker PID");
            };
            let pid: u32 = text.parse()?;
            use windows::Win32::System::Threading::{
                GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
            };
            // SAFETY: this read-only process handle identifies the live test worker
            // even if Windows later reuses its process ID.
            let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid)? };
            drop(cancel);
            let status = run.join().unwrap()?;
            let mut exit = 0;
            // SAFETY: process remains open until after GetExitCodeProcess returns.
            let queried = unsafe { GetExitCodeProcess(process, &mut exit) };
            unsafe { windows::Win32::Foundation::CloseHandle(process)? };
            queried?;
            assert_eq!(status, ExitCode::SUCCESS);
            assert_ne!(exit, 259, "worker is still active");
            Ok(())
        })
    }

    #[test]
    fn read_only_edit_retains_appended_text_and_selection() -> Result<()> {
        // SAFETY: this hidden control and all synchronous messages share the test thread.
        unsafe {
            let edit = Window(CreateWindowExW(
                Default::default(),
                w!("EDIT"),
                None,
                WS_CHILD | WINDOW_STYLE((ES_MULTILINE | ES_READONLY) as u32),
                0,
                0,
                100,
                100,
                Some(HWND_MESSAGE),
                None,
                None,
                None,
            )?);
            append(edit.0, "Un\n");
            SendMessageW(edit.0, EM_SETSEL, Some(WPARAM(0)), Some(LPARAM(2)));
            append(edit.0, "Deux\n");
            let mut text = [0u16; 24];
            let size = GetWindowTextW(edit.0, &mut text);
            assert_eq!(
                String::from_utf16(&text[..size as usize])?,
                "Un\r\nDeux\r\n"
            );
            assert_eq!(SendMessageW(edit.0, EM_GETSEL, None, None).0, 2 << 16);
        }
        Ok(())
    }
}
