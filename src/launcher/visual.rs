//! The auxiliary windows share the desktop language resources and system colours.
use std::{collections::HashMap, sync::LazyLock};
use windows::{
    Win32::{Globalization::GetUserDefaultUILanguage, System::Registry::*},
    core::w,
};

pub(crate) static MODERN: LazyLock<bool> = LazyLock::new(|| {
    std::env::var_os("VICTORY_LAUNCHER_UI")
        .or_else(|| std::env::var_os("VICTORY_LAUNCHER_V2"))
        .is_some_and(|v| v != "0")
});
pub(crate) static DARK: LazyLock<bool> = LazyLock::new(|| {
    if let Ok(s) = std::env::var("VICTORY_LAUNCHER_DARK") {
        return s == "1";
    }
    let mut light = 1u32;
    let mut size = 4;
    // This DWORD is the app theme selected in Windows Personalization.
    unsafe {
        let _ = RegGetValueW(
            HKEY_CURRENT_USER,
            w!("Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"),
            w!("AppsUseLightTheme"),
            RRF_RT_REG_DWORD,
            None,
            Some((&raw mut light).cast()),
            Some(&mut size),
        );
    }
    light == 0
});
static LANGUAGE: LazyLock<String> = LazyLock::new(|| {
    if !*MODERN || std::env::var_os("VICTORY_LAUNCHER_I18N").is_some_and(|v| v == "0") {
        return "fr".into();
    }
    let stored = || -> Option<String> {
        let path = std::path::PathBuf::from(std::env::var_os("APPDATA")?)
            .join("Velkin")
            .join("settings");
        let raw = std::fs::read(path).ok()?;
        let value: serde_json::Value = serde_json::from_slice(&raw).ok()?;
        Some(value.get("language")?.as_str()?.to_owned())
    };
    std::env::var("VICTORY_LAUNCHER_LANGUAGE")
        .ok()
        .or_else(stored)
        .filter(|code| matches!(code.as_str(), "en" | "fr" | "es" | "de" | "it" | "pt-BR"))
        .unwrap_or_else(|| {
            match unsafe { GetUserDefaultUILanguage() } & 0x3ff {
                12 => "fr",
                10 => "es",
                7 => "de",
                16 => "it",
                22 => "pt-BR",
                _ => "en",
            }
            .into()
        })
});
static STRINGS: LazyLock<HashMap<&'static str, &'static str>> = LazyLock::new(|| {
    let json = match LANGUAGE.as_str() {
        "fr" => include_str!("locales/fr.json"),
        "es" => include_str!("locales/es.json"),
        "de" => include_str!("locales/de.json"),
        "it" => include_str!("locales/it.json"),
        "pt-BR" => include_str!("locales/pt-BR.json"),
        _ => include_str!("locales/en.json"),
    };
    serde_json::from_str(json).expect("tested desktop resources")
});
pub(crate) fn t(key: &str) -> &'static str {
    STRINGS[key]
}
pub(crate) fn english() -> bool {
    LANGUAGE.as_str() == "en"
}
pub(crate) fn tone(light: u32, dark: u32) -> u32 {
    if *DARK { dark } else { light }
}

pub(crate) static RESTYLE: LazyLock<bool> = LazyLock::new(|| {
    *MODERN && std::env::var_os("VICTORY_LAUNCHER_NATIVE_STYLE").is_none_or(|v| v != "0")
});

pub(crate) fn enable_dpi() {
    if *RESTYLE {
        use windows::Win32::UI::HiDpi::*;
        // May already be set by the executable manifest.
        let _ =
            unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };
    }
}
