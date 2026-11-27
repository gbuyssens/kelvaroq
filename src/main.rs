#[cfg(windows)]
mod adapters;
#[cfg(windows)]
mod desktop;
#[cfg(windows)]
mod monitor;
#[cfg(windows)]
mod gpu;
mod image;
#[cfg(windows)]
mod native;
#[cfg(windows)]
mod preload;
#[cfg(windows)]
mod preload_process;
mod package;

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use std::{
    io::Write,
    path::PathBuf,
    process::{Command, ExitCode, Stdio},
};

#[derive(Parser)]
#[command(version, about)]
struct Args {
    #[command(subcommand)]
    command: Action,
}

#[derive(Subcommand)]
enum Action {
    /// Describe an x64 PE and its startup requirements.
    Inspect {
        image: PathBuf,
        #[arg(long)]
        json: bool,
    },
    /// Execute TLS and startup instructions in the isolated reference CPU.
    Boot(BootOptions),
    /// Run the game with the native backend, or open the reference debugger.
    Play {
        #[command(flatten)]
        options: BootOptions,
        /// Use the instrumented reference CPU even when the native backend is installed.
        #[arg(long)]
        reference: bool,
        /// Try the native renderer's experimental memory and command optimizations.
        #[arg(long, conflicts_with_all = ["reference", "python", "processor_info", "host_topology"])]
        experimental_render: bool,
    },
    /// Verify offscreen D3D12 rendering and readback on hardware adapters.
    Gpu {
        /// DXGI adapter index. By default all hardware adapters are tested.
        #[arg(long)]
        adapter: Option<u32>,
    },
}

#[derive(clap::Args)]
struct BootOptions {
    image: PathBuf,
    /// Override the interpreter included in the portable package.
    #[arg(long)]
    python: Option<PathBuf>,
    /// Virtual console profile exposed to the guest.
    #[arg(long, value_parser = ["series-x", "series-s"], default_value = "series-x")]
    console: String,
    /// Replay a raw x64 GetLogicalProcessorInformation capture.
    #[arg(long, conflicts_with = "host_topology")]
    processor_info: Option<PathBuf>,
    /// Use the Windows host topology for a diagnostic comparison.
    #[arg(long)]
    host_topology: bool,
}

impl BootOptions {
    fn run(self, windowed: bool) -> Result<ExitCode> {
        let image = package::image_path(&self.image)?;
        let plan = image::inspect(&image)?;
        let program = std::env::current_exe()?;
        let bundled_python = program.with_file_name("runtime").join("python.exe");
        let python = self.python.unwrap_or_else(|| {
            if bundled_python.is_file() {
                bundled_python
            } else {
                PathBuf::from("python")
            }
        });
        let mut command = Command::new(&python);
        command
            .env(
                "VELKIN_SIMD_LIB",
                program.with_file_name(format!(
                    "{}velkin_simd{}",
                    std::env::consts::DLL_PREFIX,
                    std::env::consts::DLL_SUFFIX
                )),
            )
            .args(["-I", "-X", "utf8", "-m", "velkin_reference"])
            .arg(&image)
            .arg(self.console)
            .stdin(Stdio::piped());
        if self.image.is_dir() {
            command.arg("--package-root").arg(&self.image);
        }
        if let Some(path) = self.processor_info {
            command.arg("--processor-info").arg(path);
        }
        if self.host_topology {
            command.arg("--host-topology");
        }
        if windowed {
            #[cfg(windows)]
            {
                command.env("VELKIN_LIVE_LOG", "1");
                return desktop::run(command, &plan);
            }
            #[cfg(not(windows))]
            bail!("the display and log windows require Windows");
        }
        let mut child = command
            .spawn()
            .with_context(|| format!("starting {}", python.display()))?;
        let write = (|| -> Result<()> {
            let mut input = child.stdin.take().context("reference CPU stdin")?;
            serde_json::to_writer(&mut input, &plan)?;
            input.flush()?;
            Ok(())
        })();
        let status = child.wait()?;
        write?;
        match status.code() {
            Some(0) => Ok(ExitCode::SUCCESS),
            Some(2) => Ok(ExitCode::from(2)),
            _ => bail!("reference CPU exited with {status}"),
        }
    }
}

fn run(args: Args) -> Result<ExitCode> {
    match args.command {
        Action::Inspect { image, json } => {
            let plan = image::inspect(&image)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&plan)?);
            } else {
                println!(
                    "PE32+ x64 | entry RVA {:#x} | {} sections | {} imports | {} TLS callbacks\nSHA-256 {}",
                    plan.entry,
                    plan.sections.len(),
                    plan.imports.len(),
                    plan.callbacks.len(),
                    plan.sha256
                );
            }
        }
        Action::Boot(options) => return options.run(false),
        Action::Play {
            options,
            reference,
            experimental_render,
        } => {
            #[cfg(windows)]
            if !reference
                && options.python.is_none()
                && options.processor_info.is_none()
                && !options.host_topology
            {
                let folder = std::env::current_exe()?.with_file_name("native");
                if let Some(engine) = native::find_engine(&folder) {
                    let image = package::image_path(&options.image)?;
                    return native::run(&engine, &image, &options.console, experimental_render);
                }
            }
            #[cfg(not(windows))]
            let _ = reference;
            if experimental_render {
                bail!("Le profil de rendu expérimental nécessite le moteur natif Windows.");
            }
            return options.run(true);
        }
        Action::Gpu { adapter } => {
            #[cfg(windows)]
            gpu::probe(adapter)?;
            #[cfg(not(windows))]
            {
                let _ = adapter;
                bail!("D3D12 requires Windows");
            }
        }
    }
    Ok(ExitCode::SUCCESS)
}

fn main() -> ExitCode {
    #[cfg(windows)]
    if let Some(result) = monitor::command() {
        return match result {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => { eprintln!("{e:#}"); ExitCode::FAILURE }
        };
    }
    match run(Args::parse()) {
        Ok(status) => status,
        Err(e) => {
            eprintln!("velkin: {e:#}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod argument_tests {
    use super::*;

    #[test]
    fn experimental_profile_is_opt_in_and_native_only() {
        for (flags, enabled) in [(vec![], false), (vec!["--experimental-render"], true)] {
            let args = Args::try_parse_from(
                ["velkin", "play"]
                    .into_iter()
                    .chain(flags)
                    .chain(["--", "jeu"]),
            )
            .expect("profil de lancement");
            assert!(
                matches!(args.command, Action::Play { experimental_render, .. } if experimental_render == enabled)
            );
        }
        for other in ["--reference", "--host-topology"] {
            assert!(
                Args::try_parse_from(["velkin", "play", "--experimental-render", other, "jeu"])
                    .is_err()
            );
        }
    }
}
