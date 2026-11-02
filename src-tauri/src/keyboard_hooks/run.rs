//! The hotkey loop: one long-lived worker thread runs capture → `POST /generate` → inject.
//!
//! A run is *claimed* (`AppState::running`) before it is queued, so a second press while one is
//! in flight is dropped rather than queued behind it (a queued run would re-capture the draft).
//! The worker owns the clipboard and keyboard handles for the app's lifetime.
//!
//! Latency notes (see [`Phases`] in the logs): `GET /config` (for `inject_mode`) runs in the
//! background while we capture instead of before it; the frontmost app comes from Accessibility
//! instead of an `osascript` subprocess; ⌘C waits are deadline polls; and the ~300ms clipboard
//! restore happens after the draft is visible, so it isn't counted in `latency_ms`.

use std::sync::OnceLock;
use std::sync::atomic::Ordering;
use std::sync::mpsc::{self, Sender};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use crate::backend_client::{self, BackendError, GenerateRequest};
use crate::{AppState, notify};

use super::capture::{CaptureOutcome, capture, make_sentinel};
use super::clipboard::{SystemClipboard, restore_clipboard};
use super::inject::{finish_inject, insert_draft};
use super::keys::SystemKeys;
use super::timing::{Phases, Timing, sleep};

pub const EVENT_STARTED: &str = "generation-started";
pub const EVENT_FINISHED: &str = "generation-finished";

/// Result of one run, as sent to the UI (`generation-finished`, `trigger_now`, `last_run`).
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RunResult {
    pub ok: bool,
    /// Hotkey press → draft in the field (or → failure).
    pub latency_ms: u64,
    pub error: Option<String>,
}

impl RunResult {
    fn ok(latency: Duration) -> Self {
        RunResult {
            ok: true,
            latency_ms: latency.as_millis() as u64,
            error: None,
        }
    }

    fn err(latency: Duration, msg: impl Into<String>) -> Self {
        RunResult {
            ok: false,
            latency_ms: latency.as_millis() as u64,
            error: Some(msg.into()),
        }
    }
}

/// Why a run stopped. Every variant injects nothing (except `RestoreFailed`, which happens after
/// the draft is in) and leaves the user's clipboard restored when possible.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoopError {
    Busy,
    NoAccessibility(String),
    /// Clipboard or keyboard simulation could not be opened.
    Device(String),
    Capture(String),
    Empty,
    Backend(BackendError),
    Inject(String),
    RestoreFailed(String),
}

impl LoopError {
    /// Short, user-visible message for the notification.
    pub fn user_message(&self) -> String {
        match self {
            LoopError::Busy => "Polytone is already running".into(),
            LoopError::NoAccessibility(msg) => msg.clone(),
            LoopError::Device(e) | LoopError::Capture(e) => e.clone(),
            LoopError::Empty => backend_client::MSG_EMPTY.into(),
            LoopError::Backend(e) => e.user_message(),
            LoopError::Inject(e) => format!("Polytone could not insert the draft: {e}"),
            LoopError::RestoreFailed(e) => {
                format!("Draft inserted, but your clipboard could not be restored: {e}")
            }
        }
    }
}

impl std::fmt::Display for LoopError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.user_message())
    }
}

impl std::error::Error for LoopError {}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

struct Job {
    reply: Option<Sender<RunResult>>,
    /// Started from Polytone's own window: wait until macOS has re-activated the previous app.
    wait_for_focus: bool,
}

static JOBS: OnceLock<Sender<Job>> = OnceLock::new();

/// Start the hotkey worker thread (once, at setup).
pub fn start_worker(app: &AppHandle) {
    let (tx, rx) = mpsc::channel::<Job>();
    if JOBS.set(tx).is_err() {
        return;
    }
    let app = app.clone();
    let spawned = thread::Builder::new()
        .name("polytone-hotkey".into())
        .spawn(move || {
            let mut devices = Devices::default();
            for job in rx {
                if job.wait_for_focus {
                    wait_for_focus_to_leave_polytone();
                }
                let r = run_claimed(&app, &mut devices);
                if let Some(reply) = job.reply {
                    let _ = reply.send(r);
                }
            }
        });
    if let Err(e) = spawned {
        log::error!("hotkey worker not started: {e}");
    }
}

/// Claim the loop and queue a run. `Err` (with the result to report) if one is already running.
pub fn request_run(
    app: &AppHandle,
    reply: Option<Sender<RunResult>>,
    wait_for_focus: bool,
) -> Result<(), RunResult> {
    let state = app.state::<AppState>();
    if state.running.swap(true, Ordering::SeqCst) {
        return Err(RunResult::err(
            Duration::ZERO,
            LoopError::Busy.user_message(),
        ));
    }
    let queued = JOBS.get().is_some_and(|tx| {
        tx.send(Job {
            reply,
            wait_for_focus,
        })
        .is_ok()
    });
    if queued {
        Ok(())
    } else {
        state.running.store(false, Ordering::SeqCst);
        Err(RunResult::err(
            Duration::ZERO,
            "Polytone's hotkey worker is not running",
        ))
    }
}

/// Clipboard + keyboard handles, opened on first use and kept.
#[derive(Default)]
struct Devices {
    clip: Option<SystemClipboard>,
    keys: Option<SystemKeys>,
}

impl Devices {
    fn get(&mut self) -> Result<(&mut SystemClipboard, &mut SystemKeys), LoopError> {
        let clip = match &mut self.clip {
            Some(c) => c,
            slot @ None => slot.insert(SystemClipboard::new().map_err(LoopError::Device)?),
        };
        let keys = match &mut self.keys {
            Some(k) => k,
            slot @ None => slot.insert(SystemKeys::new().map_err(LoopError::Device)?),
        };
        Ok((clip, keys))
    }
}

// ---------------------------------------------------------------------------
// One run
// ---------------------------------------------------------------------------

/// Run a claimed loop: emits events, updates state, logs the phase breakdown and notifies on
/// failure. Releases the claim.
fn run_claimed(app: &AppHandle, devices: &mut Devices) -> RunResult {
    let state = app.state::<AppState>();
    let _ = app.emit(EVENT_STARTED, RunResult::ok(Duration::ZERO));
    let mut phases = Phases::new();

    let result = match run_inner(app, &state, devices, &Timing::MACOS, &mut phases) {
        Ok(visible) => {
            log::info!(
                "hotkey loop ok in {}ms: {}",
                visible.as_millis(),
                phases.summary()
            );
            RunResult::ok(visible)
        }
        Err(e) => {
            log::warn!("hotkey loop failed ({e:?}): {}", phases.summary());
            notify(app, &e.user_message());
            RunResult::err(phases.elapsed(), e.user_message())
        }
    };

    *state.last_run.lock().unwrap() = Some(result.clone());
    state.running.store(false, Ordering::SeqCst);
    let _ = app.emit(EVENT_FINISHED, result.clone());
    result
}

/// Returns the time from the press until the draft was in the field.
fn run_inner(
    app: &AppHandle,
    state: &AppState,
    devices: &mut Devices,
    timing: &Timing,
    phases: &mut Phases,
) -> Result<Duration, LoopError> {
    if !crate::permissions::accessibility_trusted() {
        let _ = crate::permissions::open_permission_settings("accessibility".into());
        return Err(LoopError::NoAccessibility(
            crate::permissions::missing_accessibility_message(),
        ));
    }
    // While the user releases the keys and we capture the selection (tens to hundreds of ms),
    // let the engine re-open its API connection so /generate doesn't pay the handshake.
    warm_engine(app);
    // inject_mode is only needed after /generate; refresh it concurrently (the UI may have
    // changed it with a PUT /config that Rust never sees).
    refresh_inject_mode(app);

    let front = frontmost_app();
    phases.mark("front_app");

    wait_for_release(
        || state.trigger_held.load(Ordering::SeqCst),
        physical_modifiers_held,
        timing,
    );
    phases.mark("release");

    let hint = selection_hint();
    phases.mark("ax_hint");

    let (clip, keys) = devices.get()?;
    phases.mark("devices");

    let sentinel = make_sentinel();
    let cap = capture(clip, keys, &sentinel, timing, hint, phases).map_err(LoopError::Capture)?;
    let (intent, source) = match cap.outcome {
        CaptureOutcome::Captured { text, source } => (text, source),
        CaptureOutcome::Empty => {
            let _ = restore_clipboard(clip, &cap.saved);
            return Err(LoopError::Empty);
        }
    };
    log::info!("captured {} chars from {:?}", intent.len(), source);

    let req = GenerateRequest {
        intent,
        persona: None,
        app_context: front.as_ref().and_then(|f| f.name.clone()),
    };
    let generated = tauri::async_runtime::block_on(state.client.generate(&req));
    phases.mark("generate");
    let (draft, draft_id) = match generated {
        Ok(r) => (r.text, r.draft_id),
        Err(e) => {
            let _ = restore_clipboard(clip, &cap.saved);
            return Err(LoopError::Backend(e));
        }
    };

    let mode = *state.inject_mode.lock().unwrap();
    if let Err(e) = insert_draft(clip, keys, &draft, mode, timing) {
        let _ = restore_clipboard(clip, &cap.saved);
        return Err(LoopError::Inject(e));
    }
    phases.mark("paste");
    let visible = phases.elapsed();
    report_accepted(app, draft_id);

    // A message sent from this app soon after is (an edit of) the draft: never learn style from it.
    if let Some(f) = &front {
        crate::typing_capture::record_injection(&f.bundle_id);
    }

    finish_inject(clip, mode, &cap.saved, timing).map_err(LoopError::RestoreFailed)?;
    phases.mark("restore");
    Ok(visible)
}

/// Background `POST /warm`: the engine re-opens its model-API connection during capture.
fn warm_engine(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let client = app.state::<AppState>().client.clone();
        if let Err(e) = client.warm().await {
            log::debug!("engine warm-up request failed (generation still works): {e}");
        }
    });
}

/// Background `POST /drafts/{id}/outcome {"outcome":"accepted"}`: the draft is in the field.
/// Fire and forget (learning only; a failure is logged at debug level).
fn report_accepted(app: &AppHandle, draft_id: String) {
    if draft_id.is_empty() {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let client = app.state::<AppState>().client.clone();
        if let Err(e) = client.draft_outcome(&draft_id, "accepted").await {
            log::debug!("draft outcome not recorded: {e}");
        }
    });
}

/// Background `GET /config` → `AppState::inject_mode` (falls back to the cached value).
fn refresh_inject_mode(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let client = app.state::<AppState>().client.clone();
        match client.get_config().await {
            Ok(cfg) => *app.state::<AppState>().inject_mode.lock().unwrap() = cfg.inject_mode,
            Err(e) => log::debug!("get_config failed, using cached inject_mode: {e}"),
        }
    });
}

/// Block until the hotkey (`trigger_held`) and every modifier (`modifiers_held`) are physically
/// up, so our simulated ⌘C isn't read as ⌘⇧C; then let the key-ups reach the target app.
pub fn wait_for_release(
    trigger_held: impl Fn() -> bool,
    modifiers_held: impl Fn() -> bool,
    timing: &Timing,
) {
    let deadline = Instant::now() + timing.release_timeout;
    while trigger_held() && Instant::now() < deadline {
        sleep(timing.release_poll);
    }
    while modifiers_held() && Instant::now() < deadline {
        sleep(timing.release_poll);
    }
    sleep(timing.release_settle);
}

#[cfg(target_os = "macos")]
fn physical_modifiers_held() -> bool {
    use crate::macos::events;
    events::modifiers_held(events::modifier_flags_now())
}

#[cfg(not(target_os = "macos"))]
fn physical_modifiers_held() -> bool {
    false
}

#[cfg(target_os = "macos")]
use crate::macos::apps::frontmost_app;

#[cfg(not(target_os = "macos"))]
#[allow(dead_code)]
struct FrontApp {
    bundle_id: String,
    name: Option<String>,
}

#[cfg(not(target_os = "macos"))]
fn frontmost_app() -> Option<FrontApp> {
    None
}

#[cfg(target_os = "macos")]
fn selection_hint() -> Option<bool> {
    crate::macos::ax::focused_selection(frontmost_app().map(|a| a.pid))
}

#[cfg(not(target_os = "macos"))]
fn selection_hint() -> Option<bool> {
    None
}

/// `trigger_now` hides Polytone so macOS re-activates the app the user was in. Wait for that
/// (polling, instead of a fixed 350ms), then let its window become key.
fn wait_for_focus_to_leave_polytone() {
    #[cfg(target_os = "macos")]
    {
        let me = std::process::id() as i32;
        let deadline = Instant::now() + Duration::from_millis(600);
        while frontmost_app().is_none_or(|f| f.pid == me) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        thread::sleep(Duration::from_millis(100));
    }
    #[cfg(not(target_os = "macos"))]
    thread::sleep(Duration::from_millis(350));
}
