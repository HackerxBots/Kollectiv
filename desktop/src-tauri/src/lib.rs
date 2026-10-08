//! The Kollektiv desktop shell.
//!
//! What this crate is, and what it deliberately is not:
//!
//! * **It is a window.** The dashboard in `web/` — the same files Cloudflare
//!   Pages publishes — loads into the operating system's own webview, so the
//!   installer is a few megabytes instead of a bundled Chromium (measured
//!   numbers in `docs/performance.md`).
//! * **It is not the orchestrator.** The engine is Python (planning, dispatch,
//!   connectors, storage, budgets). Rewriting that in Rust would buy nothing —
//!   a run is model latency, not CPU — and the desktop app talks to it over HTTP
//!   exactly like the browser does. `desktop/README.md` documents the optional
//!   next step: shipping the API as a sidecar so the installer is one double-click.
//!
//! Two small conveniences that a browser tab cannot offer:
//!
//! * external links (`https://…`) open in the user's real browser, not inside
//!   the app window;
//! * the window title follows the connection state, so the taskbar says whether
//!   this instance is talking to an API.
//!
//! Everything else the shell does is `web/assets/app.js` doing its job.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde::Serialize;
use tauri::{Emitter, Manager, WebviewWindow};

/// Where the bundled API finds its data when the user configured nothing else.
const SIDECAR_HOST: &str = "127.0.0.1";
const SIDECAR_PORT: u16 = 8765;

/// The window title when no API has answered yet.
const TITLE_DISCONNECTED: &str = "Kollektiv — not connected";
/// The window title once the dashboard reports a healthy API.
const TITLE_CONNECTED: &str = "Kollektiv — mission control";

/// What the dashboard is told about its shell.
#[derive(Clone, Serialize)]
struct ShellInfo {
    shell: &'static str,
    api_major: u8,
    version: &'static str,
    /// True when this install carries its own API (the "bundle" variant).
    bundled_api: bool,
    /// Where the bundled API listens, when there is one.
    api_base: Option<String>,
    /// Why there is no bundled API, when there is none — shown in the UI, not
    /// hidden in a log, because "it did not start" is the failure people hit.
    sidecar_note: String,
}

/// The sidecar process and the note describing it, kept for the window's life.
struct Sidecar {
    child: Mutex<Option<Child>>,
    note: Mutex<String>,
}

/// Candidate paths for the bundled API executable.
///
/// Tauri puts `externalBin` next to the app binary: inside `Contents/MacOS/` in
/// a macOS bundle, beside the `.exe` on Windows, in the same directory as the
/// AppImage's payload on Linux. `binaries/` is the development fallback, so
/// `cargo tauri dev` and a checkout behave the same way.
fn sidecar_candidates() -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = Vec::new();
    let file = if cfg!(windows) { "kollektiv-api.exe" } else { "kollektiv-api" };

    if let Ok(current) = std::env::current_exe() {
        if let Some(dir) = current.parent() {
            paths.push(dir.join(file));
            paths.push(dir.join("binaries").join(file));
            // macOS .app: Contents/MacOS/.. -> Contents/Resources
            if let Some(resources) = dir.parent().map(|p| p.join("Resources")) {
                paths.push(resources.join(file));
            }
        }
    }
    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        paths.push(PathBuf::from(manifest).join("binaries").join(file));
    }
    paths
}

/// Start the bundled API, if this install has one.
///
/// Returns the shell metadata *and* the child handle, so the caller can keep the
/// process for shutdown: the API is killed when the window closes, which is what
/// stops a stale sidecar from holding its port until the next reboot.
fn start_sidecar() -> (ShellInfo, Option<Child>) {
    let candidates = sidecar_candidates();
    for candidate in &candidates {
        if !candidate.is_file() {
            continue;
        }
        let result = Command::new(candidate)
            .args([
                "--host",
                SIDECAR_HOST,
                "--port",
                &SIDECAR_PORT.to_string(),
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
        match result {
            Ok(child) => {
                println!("sidecar: started {} (pid {})", candidate.display(), child.id());
                let info = ShellInfo {
                    shell: "tauri",
                    api_major: 2,
                    version: env!("CARGO_PKG_VERSION"),
                    bundled_api: true,
                    api_base: Some(format!("http://{SIDECAR_HOST}:{SIDECAR_PORT}")),
                    sidecar_note: format!("bundled API started from {}", candidate.display()),
                };
                return (info, Some(child));
            }
            Err(error) => {
                eprintln!("sidecar: could not start {}: {error}", candidate.display());
                let info = ShellInfo {
                    shell: "tauri",
                    api_major: 2,
                    version: env!("CARGO_PKG_VERSION"),
                    bundled_api: false,
                    api_base: None,
                    sidecar_note: format!("bundled API found but not runnable: {error}"),
                };
                return (info, None);
            }
        }
    }
    (
        ShellInfo {
            shell: "tauri",
            api_major: 2,
            version: env!("CARGO_PKG_VERSION"),
            bundled_api: false,
            api_base: None,
            sidecar_note: "shell only: point the dashboard at an API you run, or build the bundle".to_string(),
        },
        None,
    )
}

/// Report what this build is, so the dashboard can offer native-only affordances
/// (and so a bug report can say which shell it came from).
#[tauri::command]
fn shell_info(state: tauri::State<'_, Sidecar>) -> serde_json::Value {
    let running = state
        .child
        .lock()
        .map(|guard| guard.is_some())
        .unwrap_or(false);
    let note = state
        .note
        .lock()
        .map(|guard| guard.clone())
        .unwrap_or_else(|_| "state unavailable".to_string());
    let info = ShellInfo {
        shell: "tauri",
        api_major: 2,
        version: env!("CARGO_PKG_VERSION"),
        bundled_api: running,
        api_base: if running {
            Some(format!("http://{SIDECAR_HOST}:{SIDECAR_PORT}"))
        } else {
            None
        },
        sidecar_note: note,
    };
    serde_json::to_value(info).unwrap_or_else(|_| serde_json::json!({"shell": "tauri"}))
}

/// Open an external URL in the user's default browser.
///
/// The dashboard calls this for support links, documentation links and the
/// "open the API docs" button — anything that is not part of the app itself.
/// Only `http` and `https` are accepted: a desktop shell must never hand
/// `mailto:`, `file:` or a custom scheme to the OS on a page's say-so.
fn external_url_allowed(url: &str) -> bool {
    url.starts_with("https://") || url.starts_with("http://")
}

#[tauri::command]
fn open_external(app: tauri::AppHandle, url: String) -> Result<(), String> {
    if !external_url_allowed(&url) {
        return Err(format!("refusing to open non-http url: {url}"));
    }
    tauri_plugin_opener::OpenerExt::opener(&app)
        .open_url(url, None::<&str>)
        .map_err(|error| error.to_string())
}

/// Reflect the dashboard's connection state in the window title.
///
/// The dashboard emits `kollektiv://connection` with `{"state": "…"}` after every
/// health check; a browser tab tells the user this with a banner, and a desktop
/// window can also tell the window manager.
#[tauri::command]
fn set_connection_state(window: WebviewWindow, state: String) -> Result<(), String> {
    let title = match state.as_str() {
        "ok" | "connected" | "healthy" => TITLE_CONNECTED,
        _ => TITLE_DISCONNECTED,
    };
    window.set_title(title).map_err(|error| error.to_string())
}

/// Start the shell.
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            shell_info,
            open_external,
            set_connection_state
        ])
        .manage(Sidecar {
            child: Mutex::new(None),
            note: Mutex::new(String::new()),
        })
        .setup(|app| {
            // The bundled API (Option 2) starts here; a shell-only install finds
            // no binary and says so, without an error dialog.
            let (info, child) = start_sidecar();
            let state = app.state::<Sidecar>();
            if let Ok(mut note) = state.note.lock() {
                *note = info.sidecar_note.clone();
            }
            if let Some(child) = child {
                if let Ok(mut guard) = state.child.lock() {
                    *guard = Some(child);
                }
            }
            app.emit("kollektiv://shell-ready", info)
                .unwrap_or_else(|error| eprintln!("could not announce the shell: {error}"));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to start the Kollektiv desktop shell")
        .run(|app, event| {
            // One window, one lifetime: when the last window closes, the bundled
            // API goes with it instead of holding its port until the next boot.
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<Sidecar>() {
                    if let Ok(mut guard) = state.child.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        });
}

#[cfg(test)]
mod tests {
    /// The shell metadata must stay parseable by the dashboard.
    #[test]
    fn shell_info_is_json() {
        let info = super::ShellInfo {
            shell: "tauri",
            api_major: 2,
            version: env!("CARGO_PKG_VERSION"),
            bundled_api: false,
            api_base: None,
            sidecar_note: "test".to_string(),
        };
        let value = serde_json::to_value(info).expect("serialisable");
        assert_eq!(value["shell"], "tauri");
        assert!(value["version"].is_string());
        assert!(value["sidecar_note"].is_string());
    }

    /// A desktop shell must not open anything but http(s) on a page's request.
    #[test]
    fn only_http_urls_are_allowed() {
        assert!(super::external_url_allowed("https://github.com/HackerxBots/Kollectiv"));
        assert!(super::external_url_allowed("http://127.0.0.1:8000/docs"));
        for bad in [
            "mailto:a@b.c",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "kollektiv://shell-ready",
            "",
        ] {
            assert!(!super::external_url_allowed(bad), "{bad} must be refused");
        }
    }

    /// The sidecar path list always includes the development location.
    #[test]
    fn sidecar_candidates_include_development_path() {
        let candidates = super::sidecar_candidates();
        assert!(!candidates.is_empty());
        assert!(candidates.iter().any(|path| path.ends_with("kollektiv-api")
            || path.ends_with("kollektiv-api.exe")));
    }
}
