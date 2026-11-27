#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

#[cfg(windows)]
#[path = "../preload.rs"]
mod preload;

#[cfg(windows)]
#[path = "../monitor/mod.rs"]
mod monitor;

#[cfg(windows)]
fn main() -> std::process::ExitCode {
    match launcher::run() {
        Ok(code) => code,
        Err(error) => {
            if std::env::args_os().any(|arg| arg == "--capture") {
                eprintln!("{error:#}");
            } else {
                launcher::error_box(&format!("{error:#}"));
            }
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(not(windows))]
fn main() -> std::process::ExitCode {
    eprintln!("Le lanceur de bureau nécessite Windows.");
    std::process::ExitCode::FAILURE
}

#[cfg(windows)]
mod launcher {
    use anyhow::{Context, Result, bail};
    use std::{
        ffi::{OsStr, OsString},
        os::windows::{ffi::OsStringExt, process::CommandExt},
        path::{Path, PathBuf},
        process::{Command, ExitCode, Stdio},
    };
    use windows::{
        Win32::{
            Foundation::{ERROR_CANCELLED, HWND, LPARAM, LRESULT, RECT, WPARAM},
            Graphics::Gdi::{COLOR_BTNFACE, DEFAULT_GUI_FONT, GetStockObject, GetSysColorBrush},
            System::{Com::*, LibraryLoader::GetModuleHandleW, Threading::DETACHED_PROCESS},
            UI::{
                Input::KeyboardAndMouse::SetFocus,
                Shell::{Common::COMDLG_FILTERSPEC, *},
                WindowsAndMessaging::*,
            },
        },
        core::{Error, HRESULT, HSTRING, w},
    };

    const PICK_FILE: usize = 101;
    const PICK_FOLDER: usize = 102;

    fn workspace_enabled(setting: Option<&OsStr>) -> bool {
        setting != Some(OsStr::new("0"))
    }

    pub(super) fn run() -> Result<ExitCode> {
        if let Some(result) = crate::monitor::command() {
            result?;
            return Ok(ExitCode::SUCCESS);
        }
        let mut args = std::env::args_os().skip(1).peekable();
        if args.next_if(|a| a == "--preload-demo").is_some() {
            if args.next_if(|a| a == "--capture").is_some() {
                let folder = args.next().context("Dossier des captures manquant")?;
                anyhow::ensure!(args.next().is_none(), "Un seul dossier attendu.");
                crate::preload::capture_demo(Path::new(&folder))?;
                return Ok(ExitCode::SUCCESS);
            }
            let percent = args
                .next()
                .map(|v| v.to_string_lossy().parse::<u32>())
                .transpose()?;
            anyhow::ensure!(
                args.next().is_none() && percent.is_none_or(|n| n <= 100),
                "Pourcentage attendu entre 0 et 100."
            );
            crate::preload::demo(percent)?;
            return Ok(ExitCode::SUCCESS);
        }
        if args.peek().is_some_and(|a| a == "--ui-demo")
            || workspace_enabled(std::env::var_os("VICTORY_LAUNCHER_V2").as_deref())
        {
            let launcher = std::env::current_exe()?;
            let args: Vec<_> = args.collect();
            let status = Command::new(launcher.with_file_name("velkin-desktop.exe"))
                .env("VICTORY_LAUNCHER_EXE", &launcher)
                .args(&args)
                .status()?;
            if status.success()
                && args.len() == 3
                && args[0] == "--ui-demo"
                && args[1] == "--capture"
            {
                for language in ["en", "fr"] {
                    for (theme, dark) in [("light", "0"), ("dark", "1")] {
                        let folder = Path::new(&args[2]).join(language).join(theme);
                        for demo in ["--preload-demo", "--monitor-demo"] {
                            let result = Command::new(&launcher)
                                .args([demo, "--capture"])
                                .arg(&folder)
                                .env("VICTORY_LAUNCHER_UI", "1")
                                .env("VICTORY_LAUNCHER_LANGUAGE", language)
                                .env("VICTORY_LAUNCHER_DARK", dark)
                                .status()?;
                            anyhow::ensure!(result.success(), "{demo} ({result})");
                        }
                    }
                }
            }
            return Ok(ExitCode::from(status.code().unwrap_or(1) as u8));
        }
        let experimental = args.next_if(|arg| arg == "--experimental-render").is_some();
        let argument = args.next();
        if args.next().is_some() {
            bail!("Ouvrez un seul dossier ou fichier .exe à la fois.");
        }
        let image = match argument {
            Some(path) => PathBuf::from(path),
            None => match choose()? {
                Some(path) => path,
                None => return Ok(ExitCode::SUCCESS),
            },
        };
        let cli = companion(&std::env::current_exe()?)?;
        let mut play = Command::new(&cli);
        play.env("VELKIN_DESKTOP_LOG", "1").arg("play");
        if experimental {
            play.arg("--experimental-render");
        }
        let result = play
            .arg("--")
            .arg(&image)
            .creation_flags(DETACHED_PROCESS.0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .output()
            .with_context(|| format!("Impossible de démarrer {}", cli.display()))?;
        match result.status.code() {
            Some(0) => Ok(ExitCode::SUCCESS),
            Some(2) => Ok(ExitCode::from(2)),
            _ => {
                let detail = String::from_utf8_lossy(&result.stderr);
                let description = detail.trim();
                if description.is_empty() {
                    bail!(
                        "Le moteur a quitté la session ({}) pour {}.",
                        result.status,
                        image.display()
                    );
                }
                bail!(
                    "Impossible de terminer la session pour {}.\n\n{description}",
                    image.display()
                )
            }
        }
    }

    fn companion(launcher: &Path) -> Result<PathBuf> {
        let directory = launcher
            .parent()
            .context("Dossier du lanceur introuvable")?;
        let packaged = directory.join("velkin-cli.exe");
        if packaged.is_file() {
            return Ok(packaged);
        }
        // The packaged launcher is itself named Velkin.exe on case-insensitive NTFS.
        let development = directory.join("velkin.exe");
        if launcher
            .file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case(OsStr::new("velkin-launcher.exe")))
            && development.is_file()
        {
            return Ok(development);
        }
        bail!(
            "Le moteur velkin-cli.exe est introuvable dans {}. Extrayez tout le paquet dans un même dossier.",
            directory.display()
        )
    }

    pub(super) fn error_box(text: &str) {
        // SAFETY: both Unicode strings remain valid throughout the modal call.
        unsafe {
            MessageBoxW(
                None,
                &HSTRING::from(text),
                w!("Velkin"),
                MB_OK | MB_ICONERROR,
            );
        }
    }

    struct Apartment;

    impl Drop for Apartment {
        fn drop(&mut self) {
            // SAFETY: this guard exists only after successful STA initialization.
            unsafe { CoUninitialize() };
        }
    }

    struct Form(HWND);

    impl Drop for Form {
        fn drop(&mut self) {
            // SAFETY: the owning thread destroys the form and its two child buttons.
            let _ = unsafe { DestroyWindow(self.0) };
        }
    }

    fn choose() -> Result<Option<PathBuf>> {
        // SAFETY: handles, registered callback, and COM dialogs all belong to the
        // same STA thread. The form guard remains alive while dispatching messages.
        unsafe {
            CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE).ok()?;
            let _apartment = Apartment;
            let instance = GetModuleHandleW(None)?.into();
            let class = w!("VelkinLaunchForm");
            let style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU;
            let cls = WNDCLASSW {
                lpfnWndProc: Some(window_proc),
                hInstance: instance,
                hCursor: LoadCursorW(None, IDC_ARROW)?,
                hbrBackground: GetSysColorBrush(COLOR_BTNFACE),
                lpszClassName: class,
                ..Default::default()
            };
            if RegisterClassW(&cls) == 0 {
                return Err(Error::from_thread().into());
            }
            let mut size = RECT {
                right: 352,
                bottom: 120,
                ..Default::default()
            };
            AdjustWindowRectEx(&mut size, style, false, WS_EX_DLGMODALFRAME)?;
            let width = size.right - size.left;
            let height = size.bottom - size.top;
            let form = Form(CreateWindowExW(
                WS_EX_DLGMODALFRAME,
                class,
                w!("Velkin — Ouvrir"),
                style,
                (GetSystemMetrics(SM_CXSCREEN) - width) / 2,
                (GetSystemMetrics(SM_CYSCREEN) - height) / 2,
                width,
                height,
                None,
                None,
                Some(instance),
                None,
            )?);
            for (id, label, top) in [
                (PICK_FILE, w!("Choisir un fichier .exe…"), 14),
                (PICK_FOLDER, w!("Choisir un dossier…"), 66),
            ] {
                let button = CreateWindowExW(
                    Default::default(),
                    w!("BUTTON"),
                    label,
                    WS_CHILD | WS_VISIBLE | WS_TABSTOP,
                    16,
                    top,
                    320,
                    40,
                    Some(form.0),
                    Some(HMENU(id as *mut _)),
                    Some(instance),
                    None,
                )?;
                SendMessageW(
                    button,
                    WM_SETFONT,
                    Some(WPARAM(GetStockObject(DEFAULT_GUI_FONT).0 as usize)),
                    Some(LPARAM(1)),
                );
            }
            let _ = ShowWindow(form.0, SW_SHOW);
            let _ = SetFocus(Some(GetDlgItem(Some(form.0), PICK_FILE as i32)?));
            let mut msg = MSG::default();
            loop {
                match GetMessageW(&mut msg, None, 0, 0).0 {
                    -1 => return Err(Error::from_thread().into()),
                    0 => return Ok(None),
                    _ => {}
                }
                if msg.hwnd == form.0 && msg.message == WM_APP {
                    if let Some(path) = pick(form.0, msg.wParam.0 == PICK_FOLDER)? {
                        return Ok(Some(path));
                    }
                } else if !IsDialogMessageW(form.0, &msg).as_bool() {
                    let _ = TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
            }
        }
    }

    unsafe extern "system" fn window_proc(
        window: HWND,
        msg: u32,
        wp: WPARAM,
        lp: LPARAM,
    ) -> LRESULT {
        // SAFETY: Win32 supplies the callback parameters. WM_APP carries only a button ID.
        unsafe {
            if msg == WM_CLOSE || msg == WM_COMMAND && wp.0 == IDCANCEL.0 as usize {
                PostQuitMessage(0);
                return LRESULT(0);
            }
            if msg == WM_COMMAND && matches!(wp.0, PICK_FILE | PICK_FOLDER) {
                let _ = PostMessageW(Some(window), WM_APP, wp, LPARAM(0));
                return LRESULT(0);
            }
            DefWindowProcW(window, msg, wp, lp)
        }
    }

    fn pick(owner: HWND, folder: bool) -> Result<Option<PathBuf>> {
        // SAFETY: COM is initialized for this thread, and Show keeps its owner alive.
        // Copy the filesystem path in UTF-16 before releasing the shell allocation.
        unsafe {
            let dialog: IFileOpenDialog =
                CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER)?;
            let options = dialog.GetOptions()? | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST;
            if folder {
                dialog.SetTitle(w!("Choisir un dossier"))?;
                dialog.SetOptions(options | FOS_PICKFOLDERS)?;
                if let Ok(start) = std::env::current_dir()
                    && start.join("MicrosoftGame.config").is_file()
                {
                    let item: IShellItem =
                        SHCreateItemFromParsingName(&HSTRING::from(start.as_os_str()), None)?;
                    dialog.SetFolder(&item)?;
                }
            } else {
                dialog.SetTitle(w!("Choisir un exécutable"))?;
                dialog.SetOptions(options | FOS_FILEMUSTEXIST)?;
                dialog.SetFileTypes(&[COMDLG_FILTERSPEC {
                    pszName: w!("Exécutables (*.exe)"),
                    pszSpec: w!("*.exe"),
                }])?;
            }
            match dialog.Show(Some(owner)) {
                Err(error) if error.code() == HRESULT::from_win32(ERROR_CANCELLED.0) => Ok(None),
                Err(error) => Err(error.into()),
                Ok(()) => {
                    let name = dialog.GetResult()?.GetDisplayName(SIGDN_FILESYSPATH)?;
                    let path = OsString::from_wide(name.as_wide());
                    CoTaskMemFree(Some(name.0.cast()));
                    Ok(Some(PathBuf::from(path)))
                }
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn workspace_defaults_on_and_zero_selects_the_picker() {
            assert!(workspace_enabled(None));
            assert!(workspace_enabled(Some(OsStr::new("1"))));
            assert!(!workspace_enabled(Some(OsStr::new("0"))));
        }

        fn desktop_test(suite: &str) {
            let exe = std::path::Path::new(env!("OUT_DIR")).join("velkin-desktop.exe");
            let result = Command::new(exe)
                .args(["--self-test", suite])
                .output()
                .unwrap();
            assert!(
                result.status.success(),
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
        }

        #[test]
        fn hardware_profiles_and_overrides() {
            desktop_test("hardware");
        }
        #[test]
        fn release_tags_and_notes() {
            desktop_test("releases");
        }
        #[test]
        fn disabled_and_daily_update_checks() {
            desktop_test("updatePolicy");
        }
        #[test]
        fn desktop_prefers_branded_engine() {
            desktop_test("engine");
        }
        #[test]
        fn interrupted_restore_recovers() {
            desktop_test("recovery");
        }
        #[test]
        fn session_snapshots_follow_process_status() {
            desktop_test("session");
        }
        #[test]
        fn child_receives_selected_options() {
            desktop_test("options");
        }
        #[test]
        fn clear_respects_storage_boundaries() {
            desktop_test("clear");
        }
        #[test]
        fn settings_migration() {
            desktop_test("settings");
        }
        #[test]
        fn manifest_selection() {
            desktop_test("paths");
        }
        #[test]
        fn snapshot_retention() {
            desktop_test("retention");
        }
        #[test]
        fn archive_round_trip_and_rejections() {
            desktop_test("archive");
        }
        #[test]
        fn storage_move_and_rollback() {
            desktop_test("move");
        }
        #[test]
        fn translation_completeness() {
            desktop_test("language");
        }

        #[test]
        fn packaged_name_never_runs_itself_and_development_prefers_packaged_cli() -> Result<()> {
            use std::{
                fs,
                time::{SystemTime, UNIX_EPOCH},
            };
            let nonce = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
            let dir = std::env::temp_dir()
                .join(format!("velkin-launcher-{}-{nonce}", std::process::id()));
            fs::create_dir(&dir)?;
            let packaged_launcher = dir.join("Velkin.exe");
            let development_launcher = dir.join("velkin-launcher.exe");
            let packaged_cli = dir.join("velkin-cli.exe");
            fs::write(&packaged_launcher, [])?;
            assert!(companion(&packaged_launcher).is_err());
            assert_eq!(companion(&development_launcher)?, dir.join("velkin.exe"));
            fs::write(&packaged_cli, [])?;
            assert_eq!(companion(&development_launcher)?, packaged_cli);
            assert_eq!(companion(&packaged_launcher)?, packaged_cli);
            fs::remove_file(packaged_cli)?;
            fs::remove_file(packaged_launcher)?;
            fs::remove_dir(dir)?;
            Ok(())
        }
    }
}
