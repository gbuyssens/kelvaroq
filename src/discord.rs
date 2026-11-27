//! Discord Rich Presence for `velkin play` started without the desktop launcher
//! (test benches, scripts, the command line), so that those windows show the
//! same activity as a game started from the launcher.
//!
//! Same protocol and Application ID as `src/launcher/Discord.cs`: the local IPC
//! pipe the Discord client opens, no network access. Best effort only: Discord
//! may be closed or slow, and the game never waits on it. The activity clears
//! itself when this process ends and the pipe closes.

#![cfg_attr(not(windows), allow(dead_code))]

use serde_json::json;
use std::io::{self, Read, Write};

/// The project's Discord application « Velkin » (same as `Presence.BuiltInAppId`).
pub(crate) const BUILT_IN_APP_ID: &str = "1575914612610572458";
const LARGE_IMAGE: &str = "velkin";
const MAX_FRAME: usize = 64 * 1024;

const HANDSHAKE: u32 = 0;
const MESSAGE: u32 = 1;
const CLOSE: u32 = 2;
const PING: u32 = 3;
const PONG: u32 = 4;

pub(crate) fn valid_app_id(id: &str) -> bool {
    (17..=20).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_digit())
}

/// `VELKIN_DISCORD=0` turns the presence off. The desktop launcher
/// (`VICTORY_LAUNCHER_UI=1`) shows its own, so the CLI stays quiet under it.
pub(crate) fn app_id(get: impl Fn(&str) -> Option<String>) -> Option<String> {
    if get("VELKIN_DISCORD").as_deref() == Some("0")
        || get("VICTORY_LAUNCHER_UI").as_deref() == Some("1")
    {
        return None;
    }
    match get("VELKIN_DISCORD_APP_ID") {
        Some(id) if valid_app_id(&id) => Some(id),
        _ => Some(BUILT_IN_APP_ID.to_owned()),
    }
}

/// Discord rejects strings shorter than 2 or longer than 128 characters.
fn field(text: &str) -> String {
    let text = text.trim();
    let mut out: String = text.chars().take(128).collect();
    if text.chars().count() > 128 {
        out = text.chars().take(127).chain(['…']).collect();
    }
    while out.chars().count() < 2 {
        out.push(' ');
    }
    out
}

pub(crate) fn activity(pid: u32, start: u64, version: &str, nonce: u64) -> String {
    json!({
        "cmd": "SET_ACTIVITY",
        "args": {
            "pid": pid,
            "activity": {
                "details": field("Un jeu console"),
                "state": field(&format!("Joue avec Velkin {version}")),
                "timestamps": { "start": start },
                "assets": { "large_image": LARGE_IMAGE, "large_text": field(&format!("Velkin {version}")) },
            },
        },
        "nonce": nonce.to_string(),
    })
    .to_string()
}

/// A frame is a little-endian opcode, a little-endian length, then UTF-8 JSON.
pub(crate) fn frame(op: u32, json: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(8 + json.len());
    out.extend_from_slice(&op.to_le_bytes());
    out.extend_from_slice(&(json.len() as u32).to_le_bytes());
    out.extend_from_slice(json.as_bytes());
    out
}

fn read_frame(stream: &mut impl Read) -> io::Result<(u32, String)> {
    let mut header = [0u8; 8];
    stream.read_exact(&mut header)?;
    let op = u32::from_le_bytes(header[..4].try_into().expect("4 bytes"));
    let length = u32::from_le_bytes(header[4..].try_into().expect("4 bytes")) as usize;
    if length > MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Discord frame too large",
        ));
    }
    let mut payload = vec![0u8; length];
    stream.read_exact(&mut payload)?;
    Ok((op, String::from_utf8_lossy(&payload).into_owned()))
}

/// Handshake, wait for READY, set the activity, then answer pings until the
/// pipe closes. Returns whether Discord accepted the activity.
pub(crate) fn session(stream: &mut (impl Read + Write), id: &str, activity: &str) -> bool {
    let mut shown = false;
    let _ = (|| -> io::Result<()> {
        stream.write_all(&frame(
            HANDSHAKE,
            &json!({ "v": 1, "client_id": id }).to_string(),
        ))?;
        stream.flush()?;
        // A refused Application ID closes with op 2.
        if read_frame(stream)?.0 != MESSAGE {
            return Ok(());
        }
        stream.write_all(&frame(MESSAGE, activity))?;
        stream.flush()?;
        loop {
            let (op, body) = read_frame(stream)?;
            match op {
                MESSAGE if body.contains("\"SET_ACTIVITY\"") && !body.contains("\"ERROR\"") => {
                    shown = true
                }
                PING => {
                    stream.write_all(&frame(PONG, &body))?;
                    stream.flush()?;
                }
                CLOSE => return Ok(()),
                _ => {}
            }
        }
    })();
    shown
}

/// Starts the presence on a background thread; never blocks nor fails.
#[cfg(windows)]
pub(crate) fn begin() {
    let Some(id) = app_id(|key| std::env::var(key).ok()) else {
        return;
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let activity = activity(
        std::process::id(),
        now.as_secs(),
        env!("CARGO_PKG_VERSION"),
        now.as_nanos() as u64,
    );
    let _ = std::thread::Builder::new()
        .name("Discord Rich Presence".into())
        .spawn(move || {
            for i in 0..10 {
                let path = format!(r"\\.\pipe\discord-ipc-{i}");
                if let Ok(mut pipe) = std::fs::OpenOptions::new()
                    .read(true)
                    .write(true)
                    .open(path)
                {
                    session(&mut pipe, &id, &activity);
                    return;
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A stand-in for the Discord client: scripted replies, recorded writes.
    struct Fake {
        input: io::Cursor<Vec<u8>>,
        written: Vec<u8>,
    }
    impl Read for Fake {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            self.input.read(buf)
        }
    }
    impl Write for Fake {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            self.written.extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn frames(mut bytes: &[u8]) -> Vec<(u32, serde_json::Value)> {
        let mut out = vec![];
        while !bytes.is_empty() {
            let (op, body) = read_frame(&mut bytes).unwrap();
            out.push((op, serde_json::from_str(&body).unwrap()));
        }
        out
    }

    #[test]
    fn app_id_rules() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            move |key: &str| {
                pairs
                    .iter()
                    .find(|(k, _)| *k == key)
                    .map(|(_, v)| v.to_string())
            }
        };
        assert_eq!(app_id(env(&[])).as_deref(), Some(BUILT_IN_APP_ID));
        assert!(valid_app_id(BUILT_IN_APP_ID));
        assert_eq!(app_id(env(&[("VELKIN_DISCORD", "0")])), None);
        assert_eq!(app_id(env(&[("VICTORY_LAUNCHER_UI", "1")])), None);
        assert_eq!(
            app_id(env(&[("VELKIN_DISCORD_APP_ID", "12345678901234567")])).as_deref(),
            Some("12345678901234567")
        );
        assert_eq!(
            app_id(env(&[("VELKIN_DISCORD_APP_ID", "pas un nombre")])).as_deref(),
            Some(BUILT_IN_APP_ID)
        );
    }

    #[test]
    fn fields_respect_discord_limits() {
        assert_eq!(field("x"), "x ");
        assert_eq!(field(&"é".repeat(300)).chars().count(), 128);
    }

    #[test]
    fn handshake_activity_and_ping() {
        let mut input = frame(MESSAGE, r#"{"cmd":"DISPATCH","evt":"READY"}"#);
        input.extend(frame(MESSAGE, r#"{"cmd":"SET_ACTIVITY","evt":null}"#));
        input.extend(frame(PING, r#"{"n":1}"#));
        let mut fake = Fake {
            input: io::Cursor::new(input),
            written: vec![],
        };
        let activity = activity(42, 1000, "0.2.0", 7);
        assert!(session(&mut fake, BUILT_IN_APP_ID, &activity));
        let sent = frames(&fake.written);
        assert_eq!(sent.len(), 3);
        assert_eq!(sent[0].0, HANDSHAKE);
        assert_eq!(sent[0].1["client_id"], BUILT_IN_APP_ID);
        assert_eq!(sent[1].0, MESSAGE);
        assert_eq!(sent[1].1["args"]["pid"], 42);
        assert_eq!(
            sent[1].1["args"]["activity"]["assets"]["large_image"],
            "velkin"
        );
        assert_eq!(sent[2], (PONG, json!({ "n": 1 })));
    }

    #[test]
    fn refused_application_id_stops() {
        let mut fake = Fake {
            input: io::Cursor::new(frame(CLOSE, r#"{"code":4000}"#)),
            written: vec![],
        };
        assert!(!session(&mut fake, BUILT_IN_APP_ID, "{}"));
        assert_eq!(frames(&fake.written).len(), 1);
    }
}
