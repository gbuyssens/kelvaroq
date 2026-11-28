use anyhow::{Context, Result, bail};
use std::{
    fs::File,
    path::Path,
    process::{Command, ExitCode},
};
use windows::{
    Win32::System::Console::{AllocConsole, SetConsoleTitleW},
    core::w,
};

/// What the engine's own `play` keeps from the environment (its `KEPT`):
/// every other `VICTORY_*` variable is cleared there, so Velkin hands those
/// over as `--set NAME=VALUE`.
const ENGINE_KEEPS: &[&str] = &[
    "VICTORY_HOME",
    "VICTORY_ADAPTER",
    "VICTORY_CONFIG",
    "VICTORY_GAME_DIR",
    "VICTORY_CACHE_DIR",
    "VICTORY_DATA_DIR",
    "VICTORY_STREAMLINE_DIR",
    "VICTORY_FIDELITYFX_DIR",
    "VICTORY_LOG",
];

/// The `VICTORY_*` switches set in `vars` that the engine's `play` would
/// otherwise clear, as `--set` arguments, in a stable order.
fn switches(vars: impl Iterator<Item = (std::ffi::OsString, std::ffi::OsString)>) -> Vec<String> {
    let mut set: Vec<String> = vars
        .filter_map(|(key, value)| Some((key.into_string().ok()?, value.into_string().ok()?)))
        .filter(|(key, _)| key.starts_with("VICTORY_") && !ENGINE_KEEPS.contains(&key.as_str()))
        .map(|(key, value)| format!("{key}={value}"))
        .collect();
    set.sort();
    set.into_iter()
        .flat_map(|pair| ["--set".to_owned(), pair])
        .collect()
}

/// Approach after XCL b49be18: resolve the host executable relative to the launcher.
pub(crate) fn find_engine(folder: &Path) -> Option<std::path::PathBuf> {
    ["velkin-engine.exe", "victory.exe"]
        .iter()
        .map(|name| folder.join(name))
        .find(|path| path.is_file())
}

pub(crate) fn run(
    engine: &Path,
    image: &Path,
    console: &str,
    experimental_render: bool,
) -> Result<ExitCode> {
    // Give the native file services a conventional DOS path when it names
    // the same file. Keep the extended form for paths that require it.
    let image = dunce::canonicalize(image).context("ouverture de l’exécutable du jeu")?;
    let desktop_v2 = std::env::var_os("VICTORY_LAUNCHER_UI").is_some_and(|v| v == "1");
    if experimental_render {
        // The profile named switches of the earlier engine line (victory-fork
        // up to 9a9091d); the engine since 2026-11-28 (VIctory-NVIDIA) has its
        // own settings, which `play` applies and its F1 menu changes.
        eprintln!(
            "--experimental-render ne change plus rien : le moteur applique ses propres réglages (menu F1)."
        );
    }
    let configure = || -> Result<Command> {
        let mut game = Command::new(engine);
        game.current_dir(engine.parent().context("dossier du moteur natif")?)
            .env(
                "VICTORY_GAME_DIR",
                if desktop_v2 {
                    std::env::var_os("VICTORY_GAME_DIR")
                } else {
                    None
                }
                .unwrap_or_else(|| image.parent().unwrap_or(&image).as_os_str().to_owned()),
            );
        Ok(game)
    };
    if std::env::var_os("VELKIN_DESKTOP_LOG").is_some()
        && let Err(error) = crate::preload_process::before_play(&configure)
    {
        eprintln!("Préchargement interrompu. {error:#}");
    }
    let mut game = configure()?;
    // `play`, not `run`: the engine's launcher applies the settings it is
    // measured with (and those its F1 menu saved) before it runs the title.
    game.arg("play").arg(&image);
    if console == "series-s" {
        game.arg("--series-s");
    }
    game.args(switches(std::env::vars_os()));
    if std::env::var_os("VELKIN_DESKTOP_LOG").is_some() {
        // The desktop launcher starts this CLI without a console and captures its
        // stderr for the error dialog. Give the child its own visible log output.
        unsafe {
            AllocConsole().context("ouverture du journal Windows")?;
            if desktop_v2 {
                let caption = format!("Velkin | {}", crate::preload::visual::t("journal"));
                SetConsoleTitleW(&windows::core::HSTRING::from(caption))?;
            } else {
                SetConsoleTitleW(w!("Velkin | Journal du jeu"))?;
            }
        }
        let log = File::options().write(true).open("CONOUT$")?;
        game.stdout(log.try_clone()?).stderr(log);
    }
    // Benches and scripts start this CLI directly: give their windows the
    // launcher's Discord activity too (the launcher shows its own).
    crate::discord::begin();
    let mut child = game
        .spawn()
        .with_context(|| format!("démarrage de {}", engine.display()))?;
    let mut monitor =
        if desktop_v2 && std::env::var_os("VICTORY_LAUNCHER_MONITOR").is_some_and(|v| v == "1") {
            std::env::var_os("VICTORY_LAUNCHER_EXE").and_then(|exe| {
                Command::new(exe)
                    .arg("--monitor")
                    .arg("--pid")
                    .arg(child.id().to_string())
                    .spawn()
                    .ok()
            })
        } else {
            None
        };
    let status = child.wait();
    if let Some(monitor) = monitor.as_mut() {
        let _ = monitor.kill();
        let _ = monitor.wait();
    }
    let status = status?;
    if !status.success() {
        bail!("Le moteur natif s’est arrêté ({status}).");
    }
    Ok(ExitCode::SUCCESS)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hands_over_the_switches_play_would_clear() {
        let vars = [
            ("VICTORY_KEYS", "20:Enter"),
            ("VICTORY_GAME_DIR", r"C:\jeu"),
            ("PATH", r"C:\Windows"),
            ("VICTORY_BVH_BUILDER", "0"),
        ]
        .map(|(k, v)| (k.into(), v.into()));
        assert_eq!(
            switches(vars.into_iter()),
            [
                "--set",
                "VICTORY_BVH_BUILDER=0",
                "--set",
                "VICTORY_KEYS=20:Enter"
            ]
        );
    }

    #[test]
    fn finds_the_packaged_engine_and_older_installations() -> Result<()> {
        let folder =
            std::env::temp_dir().join(format!("velkin-engine-test-{}", std::process::id()));
        std::fs::create_dir(&folder)?;
        let legacy = folder.join("victory.exe");
        let current = folder.join("velkin-engine.exe");
        assert_eq!(find_engine(&folder), None);
        std::fs::write(&legacy, [])?;
        assert_eq!(find_engine(&folder), Some(legacy.clone()));
        std::fs::write(&current, [])?;
        assert_eq!(find_engine(&folder), Some(current.clone()));
        std::fs::remove_file(legacy)?;
        std::fs::remove_file(current)?;
        std::fs::remove_dir(folder)?;
        Ok(())
    }
}
