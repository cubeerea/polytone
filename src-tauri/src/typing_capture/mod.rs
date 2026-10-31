//! Typing capture: learn from messages the user types and sends in allowlisted apps
//! (CONTRACTS.md "Typing capture"). Opt-in, default OFF, local only.
//!
//! Pipeline:
//! 1. [`tap`]: a listen-only `CGEventTap` (keyDown) on its own thread. The callback only
//!    forwards `(keycode, flags)` over a channel (re-enabling a timed-out tap) — it never blocks.
//! 2. [`worker`]: gates every key: capture enabled, hotkey not paused, secure input off,
//!    frontmost app allowlisted. Edit keys schedule a debounced ([`DEBOUNCE`]) read of the focused
//!    element's `AXValue`, cached per app. Password fields (`AXSecureTextField`) are never read.
//! 3. A send key ([`classify_key`]: Return without Shift in chat-like apps, Cmd+Return, Cmd+Shift+D
//!    in Mail) takes the cached text (or a fresher immediate read of the same element) and then
//!    confirms the send ([`confirm_sent`]): the field must clear or lose focus within ~1s. A Return
//!    that just inserts a newline (a browser textarea, a long email) is therefore never captured.
//! 4. Filters ([`prepare_text`], [`SentLog`]): ≥ [`MIN_WORDS`] words, capped at [`MAX_CHARS`],
//!    identical text per app is never resent. A send within [`INJECT_WINDOW`] of a Polytone inject
//!    in the same app ([`InjectionTracker`]) is posted with `after_inject=true`, which the backend
//!    stores for edit learning and never learns style from.
//! 5. `POST /ingest/typed` fire-and-forget on the async runtime; failures are logged and shown in
//!    [`CaptureStatus::last_error`], never as per-item notifications.
//!
//! Text is never captured on app switch (that would grab half-written drafts) — only on sends.
//! The decision logic lives in [`policy`].

pub mod policy;
#[cfg(target_os = "macos")]
mod tap;
#[cfg(target_os = "macos")]
mod worker;

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Instant, SystemTime};

use serde::Serialize;
use tauri::{AppHandle, Manager};

use crate::AppState;
use crate::backend_client::{BackendConfig, TypedIngestRequest};

pub use policy::*;

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CaptureStatus {
    pub enabled: bool,
    pub running: bool,
    pub apps: Vec<String>,
    pub captured_this_session: u32,
    pub last_captured_at: Option<String>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RunningApp {
    pub bundle_id: String,
    pub name: String,
}

#[derive(Default)]
struct Shared {
    enabled: AtomicBool,
    running: AtomicBool,
    captured: AtomicU32,
    status: Mutex<StatusInner>,
    injections: Mutex<InjectionTracker>,
}

#[derive(Default)]
struct StatusInner {
    apps: Vec<String>,
    last_captured_at: Option<String>,
    last_error: Option<String>,
}

static SHARED: OnceLock<Shared> = OnceLock::new();
static APP: OnceLock<AppHandle> = OnceLock::new();

fn shared() -> &'static Shared {
    SHARED.get_or_init(Shared::default)
}

fn set_error(msg: Option<String>) {
    shared().status.lock().unwrap().last_error = msg;
}

pub fn status() -> CaptureStatus {
    let s = shared();
    let st = s.status.lock().unwrap();
    CaptureStatus {
        enabled: s.enabled.load(Ordering::SeqCst),
        running: s.running.load(Ordering::SeqCst),
        apps: st.apps.clone(),
        captured_this_session: s.captured.load(Ordering::SeqCst),
        last_captured_at: st.last_captured_at.clone(),
        last_error: st.last_error.clone(),
    }
}

#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn allowlisted(bundle_id: &str) -> bool {
    shared()
        .status
        .lock()
        .unwrap()
        .apps
        .iter()
        .any(|a| a == bundle_id)
}

/// Trim, drop blanks and duplicates (keeping order).
pub fn clean_apps(apps: Vec<String>) -> Vec<String> {
    let mut cleaned: Vec<String> = Vec::new();
    for a in apps.into_iter().map(|a| a.trim().to_string()) {
        if !a.is_empty() && !cleaned.contains(&a) {
            cleaned.push(a);
        }
    }
    cleaned
}

/// Apply `enabled` / `apps`: start the tap (retrying a failed start) or stop it.
/// Blocks up to ~2s while the tap thread starts or stops: call it off the main thread.
pub fn apply(app: &AppHandle, enabled: bool, apps: Vec<String>) -> CaptureStatus {
    let _ = APP.set(app.clone());
    let s = shared();
    let was_running = s.running.load(Ordering::SeqCst);
    s.status.lock().unwrap().apps = clean_apps(apps);
    s.enabled.store(enabled, Ordering::SeqCst);
    if enabled {
        match platform::start() {
            Ok(()) => {
                if !crate::permissions::accessibility_trusted() {
                    set_error(Some(
                        "Accessibility permission is needed to read what you type \
                         (System Settings > Privacy & Security > Accessibility)"
                            .into(),
                    ));
                } else {
                    set_error(None);
                }
            }
            Err(e) => {
                log::warn!("typing capture could not start: {e}");
                set_error(Some(e));
            }
        }
    } else {
        platform::stop();
        set_error(None);
    }
    let st = status();
    crate::security::audit_capture_transition(app, was_running, &st);
    st
}

/// Called from `sync_from_backend_config` with the backend's `/config` (blocking; see [`apply`]).
pub fn apply_backend_config(app: &AppHandle, cfg: &BackendConfig) {
    let enabled = cfg.capture_enabled.unwrap_or(false);
    let apps = cfg.capture_apps.clone().unwrap_or_default();
    if enabled || shared().enabled.load(Ordering::SeqCst) {
        let st = apply(app, enabled, apps);
        log::info!(
            "typing capture: enabled={} running={}",
            st.enabled,
            st.running
        );
    } else {
        shared().status.lock().unwrap().apps = clean_apps(apps);
    }
}

/// Record that Polytone just injected a draft into the app `bundle_id`.
pub fn record_injection(bundle_id: &str) {
    shared()
        .injections
        .lock()
        .unwrap()
        .record(bundle_id, Instant::now());
}

#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn after_inject(bundle_id: &str) -> bool {
    shared()
        .injections
        .lock()
        .unwrap()
        .after_inject(bundle_id, Instant::now())
}

#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn paused() -> bool {
    APP.get()
        .is_some_and(|a| a.state::<AppState>().paused.load(Ordering::SeqCst))
}

/// Fire-and-forget `POST /ingest/typed`.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn dispatch(req: TypedIngestRequest) {
    let Some(app) = APP.get() else { return };
    let client = app.state::<AppState>().client.clone();
    tauri::async_runtime::spawn(async move {
        match client.ingest_typed(&req).await {
            Ok(res) => {
                let s = shared();
                s.captured.fetch_add(1, Ordering::SeqCst);
                let mut st = s.status.lock().unwrap();
                st.last_captured_at = Some(iso_utc(SystemTime::now()));
                if st
                    .last_error
                    .as_deref()
                    .is_some_and(|e| e.starts_with("could not send"))
                {
                    st.last_error = None;
                }
                log::info!(
                    "typed capture from {} ({} chars, after_inject={}): accepted={} rejected={} {:?}",
                    req.app,
                    req.text.chars().count(),
                    req.after_inject,
                    res.accepted,
                    res.rejected,
                    res.reasons
                );
            }
            Err(e) => {
                log::warn!("typed capture POST failed: {e}");
                set_error(Some(format!("could not send captured text: {e}")));
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Tauri commands (names / shapes consumed by src/tauri.ts)
// ---------------------------------------------------------------------------

/// Async so starting / stopping the tap (up to ~2s) never blocks the main thread.
#[tauri::command]
pub async fn set_capture(app: AppHandle, enabled: bool, apps: Vec<String>) -> CaptureStatus {
    tauri::async_runtime::spawn_blocking(move || apply(&app, enabled, apps))
        .await
        .unwrap_or_else(|_| status())
}

#[tauri::command]
pub fn get_capture_status() -> CaptureStatus {
    status()
}

#[tauri::command]
pub fn list_running_apps() -> Vec<RunningApp> {
    platform::running_apps()
        .into_iter()
        .map(|(bundle_id, name)| RunningApp { bundle_id, name })
        .collect()
}

// ---------------------------------------------------------------------------
// Platform
// ---------------------------------------------------------------------------

#[cfg(target_os = "macos")]
mod platform {
    pub use super::tap::{start, stop};
    pub use crate::macos::apps::running_apps;
}

#[cfg(not(target_os = "macos"))]
mod platform {
    pub fn start() -> Result<(), String> {
        Err("typing capture is only available on macOS".into())
    }

    pub fn stop() {}

    pub fn running_apps() -> Vec<(String, String)> {
        Vec::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_defaults_off() {
        let s = status();
        assert!(!s.running);
        assert_eq!(s.captured_this_session, 0);
    }

    #[test]
    fn apps_are_cleaned() {
        assert_eq!(
            clean_apps(vec![
                " com.apple.mail ".into(),
                "".into(),
                "com.apple.mail".into(),
                "notion.id".into()
            ]),
            vec!["com.apple.mail".to_string(), "notion.id".into()]
        );
    }

    /// Manual: `cargo test -- --ignored --nocapture macos_smoke` (needs a GUI session; the tap
    /// part needs Input Monitoring / Accessibility for the binary running the test).
    #[cfg(target_os = "macos")]
    #[test]
    #[ignore]
    fn macos_smoke() {
        let apps = platform::running_apps();
        println!("running apps: {}", apps.len());
        assert!(!apps.is_empty());
        println!(
            "frontmost: {:?}",
            crate::macos::apps::frontmost_app().map(|f| f.bundle_id)
        );
        match platform::start() {
            Ok(()) => {
                assert!(status().running);
                platform::stop();
                assert!(!status().running);
                println!("tap: started and stopped");
            }
            Err(e) => println!("tap failed (expected without permission): {e}"),
        }
    }
}
