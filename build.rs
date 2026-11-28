use std::{env, path::PathBuf, process::Command};

fn main() {
    println!("cargo:rerun-if-changed=src/launcher");
    println!("cargo:rerun-if-changed=assets/launcher/mark.png");
    println!("cargo:rerun-if-changed=scripts/build_desktop.py");
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let status = Command::new("python")
        .arg("scripts/build_desktop.py")
        .arg(&out)
        .arg(env!("CARGO_PKG_VERSION"))
        .status()
        .expect("building Windows desktop resources requires Python 3");
    assert!(status.success(), "Windows desktop resource build failed");
    println!(
        "cargo:rustc-link-arg-bin=velkin-launcher={}",
        out.join("launcher.res").display()
    );
}
