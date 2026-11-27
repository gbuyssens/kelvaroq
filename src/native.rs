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

// A process-local test profile. An explicit per-feature setting always wins.
const EXPERIMENTAL_RENDER: &[(&str, &str)] = &[
    ("VICTORY_GPU_UPLOAD_READONLY_MB", "256"),
    ("VICTORY_CARD_READONLY_MB", "128"),
    ("VICTORY_RECYCLE_COMPARE_SLOTS", "1"),
    ("VICTORY_GPU_CONSTANT_FILLS", "1"),
    ("VICTORY_TEXTURE_NO_WAIT", "1"),
    ("VICTORY_TEXTURE_SCOPED_REMAP", "1"),
    ("VICTORY_DESCRIPTOR_SYNC_BATCH", "1"),
    ("VICTORY_ROOT_BIND_CACHE", "1"),
    ("VICTORY_RELAY_COPY_RUNS", "1"),
    ("VICTORY_PIPELINE_EARLY_WITHOUT_LIBRARY", "1"),
    ("VICTORY_SCOPED_CARD_BARRIERS", "1"),
];

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
    let configure = || -> Result<Command> {
        let mut game = Command::new(engine);
        if experimental_render {
            game.envs(
                EXPERIMENTAL_RENDER
                    .iter()
                    .copied()
                    .filter(|(key, _)| std::env::var_os(key).is_none()),
            );
        }
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
    game.arg("run")
        .arg(&image)
        .args(["--console", console, "--execute"]);
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
