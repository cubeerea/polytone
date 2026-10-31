//! App token, private data files, and the UI lock (CONTRACTS.md "Security").
//!
//! - The **app token** authenticates Rust to the backend. Rust generates it when it spawns the
//!   backend (`POLYTONE_API_TOKEN`), or reads `<data_dir>/.api_token` when it attaches to one
//!   that is already running. It never reaches the webview.
//! - The UI gets a short-lived **session token** only after [`unlock`] (Touch ID / Mac password
//!   via LocalAuthentication, then `POST /session`). [`lock`] revokes it; so do screen lock,
//!   sleep and the tray's "Lock Polytone". The hotkey and typing capture keep working while
//!   locked: they use the app token.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use base64::Engine as _;
use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};

use crate::AppState;

pub const TOKEN_FILE: &str = ".api_token";
pub const EVENT_LOCKED: &str = "locked";

/// Shown in the system prompt as `"Polytone" is trying to <reason>.`
const UNLOCK_REASON: &str = "unlock your drafts and writing style";
/// How long an unanswered prompt may stay up before we cancel it.
const PROMPT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(300);

// ---------------------------------------------------------------------------
// App token
// ---------------------------------------------------------------------------

/// 32 random bytes from the OS CSPRNG, base64url without padding (43 chars).
pub fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).expect("OS random number generator unavailable");
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

/// Constant-time check of the backend's `/health?challenge=` answer (see
/// `BackendClient::verify`).
pub fn health_proof_matches(token: &str, challenge: &str, proof_hex: &str) -> bool {
    use hmac::{Hmac, Mac};
    let Ok(proof) = hex::decode(proof_hex) else {
        return false;
    };
    let mut mac = Hmac::<sha2::Sha256>::new_from_slice(token.as_bytes()).expect("any key length");
    mac.update(b"polytone-health-v1:");
    mac.update(challenge.as_bytes());
    mac.verify_slice(&proof).is_ok()
}

/// The token in `path`, trimmed; `None` if missing, unreadable or blank.
pub fn read_token_file(path: &Path) -> Option<String> {
    let s = std::fs::read_to_string(path).ok()?;
    let t = s.trim();
    (!t.is_empty()).then(|| t.to_string())
}

// ---------------------------------------------------------------------------
// Data dir + private files
// ---------------------------------------------------------------------------

/// `$POLYTONE_DATA_DIR` (relative = against the repo root, like the backend run from there),
/// else `<repo>/data`.
pub fn resolve_data_dir(env_dir: Option<String>, repo_root: Option<&Path>) -> Option<PathBuf> {
    match env_dir.filter(|d| !d.trim().is_empty()) {
        Some(d) => {
            let p = PathBuf::from(d.trim());
            if p.is_absolute() {
                Some(p)
            } else {
                repo_root.map(|r| r.join(p))
            }
        }
        None => repo_root.map(|r| r.join("data")),
    }
}

/// Create `dir` (and parents) if missing; a directory we create is 0700.
pub fn ensure_private_dir(dir: &Path) -> io::Result<()> {
    if dir.is_dir() {
        return Ok(());
    }
    let mut b = std::fs::DirBuilder::new();
    b.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        b.mode(0o700);
    }
    b.create(dir)?;
    #[cfg(unix)]
    {
        // `mode` is filtered by the umask; set it exactly.
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// Open `path` for appending, creating it 0600; an existing file is tightened to 0600.
pub fn open_private_append(path: &Path) -> io::Result<std::fs::File> {
    let mut o = std::fs::OpenOptions::new();
    o.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        o.mode(0o600);
    }
    let f = o.open(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        f.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(f)
}

// ---------------------------------------------------------------------------
// Lock state machine
// ---------------------------------------------------------------------------

/// UI lock state. Starts locked. A lock that happens while an unlock prompt is up wins:
/// the unlock's result is discarded (its fresh session revoked) when it finally returns.
#[derive(Debug, Default)]
pub struct LockMachine {
    unlocked: bool,
    unlocking: bool,
    /// Bumped by every lock; an unlock only lands if no lock happened since it began.
    generation: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UnlockTicket(u64);

impl LockMachine {
    pub fn locked(&self) -> bool {
        !self.unlocked
    }

    /// Start an unlock; `None` if one is already in progress (one prompt at a time).
    pub fn begin_unlock(&mut self) -> Option<UnlockTicket> {
        if self.unlocking {
            return None;
        }
        self.unlocking = true;
        Some(UnlockTicket(self.generation))
    }

    /// End an unlock. Returns true if the app is now unlocked; false if it failed or a lock
    /// arrived in the meantime (the caller must then revoke any session it created).
    pub fn finish_unlock(&mut self, ticket: UnlockTicket, success: bool) -> bool {
        self.unlocking = false;
        let ok = success && ticket.0 == self.generation;
        if ok {
            self.unlocked = true;
        }
        ok
    }

    /// Lock. Returns true if this changed anything (was unlocked, or an unlock was pending).
    pub fn lock(&mut self) -> bool {
        let changed = self.unlocked || self.unlocking;
        self.unlocked = false;
        self.generation += 1;
        changed
    }
}

/// Why we locked; sent with the `locked` event and in the audit detail.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum LockReason {
    /// The UI asked (button or its idle timer).
    Ui,
    Idle,
    Tray,
    Screen,
    Sleep,
    /// The backend said the session expired (the UI saw a 401): nothing to revoke.
    Expired,
}

impl LockReason {
    pub fn parse(s: Option<&str>) -> Self {
        match s.map(str::trim) {
            Some("idle") => Self::Idle,
            Some("tray") => Self::Tray,
            Some("screen") => Self::Screen,
            Some("sleep") => Self::Sleep,
            Some("expired") => Self::Expired,
            _ => Self::Ui,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ui => "ui",
            Self::Idle => "idle",
            Self::Tray => "tray",
            Self::Screen => "screen",
            Self::Sleep => "sleep",
            Self::Expired => "expired",
        }
    }
}

// ---------------------------------------------------------------------------
// LocalAuthentication errors → one calm sentence
// ---------------------------------------------------------------------------

/// `LAError` codes (LAError.h).
pub mod la {
    pub const AUTHENTICATION_FAILED: isize = -1;
    pub const USER_CANCEL: isize = -2;
    pub const USER_FALLBACK: isize = -3;
    pub const SYSTEM_CANCEL: isize = -4;
    pub const PASSCODE_NOT_SET: isize = -5;
    pub const BIOMETRY_NOT_AVAILABLE: isize = -6;
    pub const BIOMETRY_NOT_ENROLLED: isize = -7;
    pub const BIOMETRY_LOCKOUT: isize = -8;
    pub const APP_CANCEL: isize = -9;
    pub const INVALID_CONTEXT: isize = -10;
    pub const NOT_INTERACTIVE: isize = -1004;
}

/// Short machine name for the audit log (never the system's localized text).
pub fn auth_error_kind(code: isize) -> &'static str {
    match code {
        la::AUTHENTICATION_FAILED => "failed",
        la::USER_CANCEL | la::USER_FALLBACK => "cancelled",
        la::SYSTEM_CANCEL | la::APP_CANCEL => "interrupted",
        la::PASSCODE_NOT_SET => "no_password",
        la::BIOMETRY_NOT_AVAILABLE => "no_biometrics",
        la::BIOMETRY_NOT_ENROLLED => "not_enrolled",
        la::BIOMETRY_LOCKOUT => "lockout",
        la::NOT_INTERACTIVE => "not_interactive",
        _ => "error",
    }
}

/// User-facing line under the Unlock button.
pub fn auth_error_message(code: isize) -> String {
    match code {
        la::AUTHENTICATION_FAILED => "That didn't match. Try again.".into(),
        la::USER_CANCEL | la::USER_FALLBACK => "Unlock cancelled.".into(),
        la::SYSTEM_CANCEL | la::APP_CANCEL => "Unlock was interrupted. Try again.".into(),
        la::PASSCODE_NOT_SET => {
            "Set a login password for this Mac (System Settings > Users & Groups) to unlock Polytone."
                .into()
        }
        la::BIOMETRY_NOT_AVAILABLE => {
            "Touch ID isn't available. Use your Mac password when asked.".into()
        }
        la::BIOMETRY_NOT_ENROLLED => {
            "Touch ID isn't set up on this Mac. Use your Mac password when asked.".into()
        }
        la::BIOMETRY_LOCKOUT => {
            "Touch ID is locked after too many tries. Enter your Mac password to unlock.".into()
        }
        la::NOT_INTERACTIVE | la::INVALID_CONTEXT => {
            "Polytone couldn't show the unlock prompt. Bring the window to the front and try again."
                .into()
        }
        other => format!("Unlock failed (error {other})."),
    }
}

// ---------------------------------------------------------------------------
// Audit (fire and forget)
// ---------------------------------------------------------------------------

/// `POST /audit/event` in the background; failures are only logged.
pub fn audit(app: &AppHandle, event: &'static str, detail: serde_json::Value) {
    let client = app.state::<AppState>().client.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(e) = client.audit_event(event, &detail).await {
            log::debug!("audit {event} not recorded: {e}");
        }
    });
}

/// `capture.start` / `capture.stop` when the typing-capture tap starts or stops (app count only).
pub fn audit_capture_transition(
    app: &AppHandle,
    was_running: bool,
    now: &crate::typing_capture::CaptureStatus,
) {
    let event = match (was_running, now.running) {
        (false, true) => "capture.start",
        (true, false) => "capture.stop",
        _ => return,
    };
    audit(app, event, serde_json::json!({ "apps": now.apps.len() }));
}

// ---------------------------------------------------------------------------
// Lock / unlock
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct LockedEvent {
    pub reason: LockReason,
}

#[derive(Debug, Clone, Serialize)]
pub struct LockStateDto {
    pub locked: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct UnlockResult {
    pub token: String,
    pub idle_timeout_s: u64,
}

fn machine(app: &AppHandle) -> &Mutex<LockMachine> {
    &app.state::<AppState>().inner().lock.inner
}

/// Lock now: revoke every UI session, audit, and tell the UI. Safe to call from any thread.
pub fn lock_app(app: &AppHandle, reason: LockReason) {
    let changed = machine(app).lock().unwrap().lock();
    if reason == LockReason::Expired {
        // The backend already dropped the session (and audits `session.expired`).
        return;
    }
    let client = app.state::<AppState>().client.clone();
    tauri::async_runtime::spawn(async move {
        // Always revoke (cheap, idempotent), even if we thought we were locked.
        if let Err(e) = client.revoke_sessions().await {
            log::warn!("could not revoke UI sessions: {e}");
        }
        let detail = serde_json::json!({ "reason": reason.as_str() });
        if changed && let Err(e) = client.audit_event("session.lock", &detail).await {
            log::debug!("audit session.lock not recorded: {e}");
        }
    });
    if changed {
        log::info!("locked ({})", reason.as_str());
        let _ = app.emit(EVENT_LOCKED, LockedEvent { reason });
    }
}

#[cfg(target_os = "macos")]
fn device_owner_auth() -> Result<(), (isize, String)> {
    crate::macos::local_auth::authenticate(UNLOCK_REASON, PROMPT_TIMEOUT)
        .map_err(|f| (f.code, f.description))
}

#[cfg(not(target_os = "macos"))]
fn device_owner_auth() -> Result<(), (isize, String)> {
    let _ = (UNLOCK_REASON, PROMPT_TIMEOUT);
    Err((la::BIOMETRY_NOT_AVAILABLE, "unsupported platform".into()))
}

/// Touch ID / Mac password, then `POST /session`. The prompt waits on a blocking-pool thread,
/// never on the main thread.
#[tauri::command]
pub async fn unlock(app: AppHandle) -> Result<UnlockResult, String> {
    let ticket = machine(&app)
        .lock()
        .unwrap()
        .begin_unlock()
        .ok_or_else(|| "Unlock is already in progress.".to_string())?;
    let finish = |ok: bool| machine(&app).lock().unwrap().finish_unlock(ticket, ok);

    let auth = tauri::async_runtime::spawn_blocking(device_owner_auth)
        .await
        .unwrap_or_else(|e| Err((la::APP_CANCEL, e.to_string())));
    if let Err((code, desc)) = auth {
        finish(false);
        log::info!("unlock failed: {code} ({desc})");
        audit(
            &app,
            "session.unlock_failed",
            serde_json::json!({ "reason": auth_error_kind(code), "code": code }),
        );
        return Err(auth_error_message(code));
    }

    let client = app.state::<AppState>().client.clone();
    let session = match client.create_session().await {
        Ok(s) => s,
        Err(e) => {
            finish(false);
            audit(
                &app,
                "session.unlock_failed",
                serde_json::json!({ "reason": "backend" }),
            );
            return Err(match e {
                crate::backend_client::BackendError::NotRunning => {
                    "Polytone's engine isn't running.".to_string()
                }
                other => other.user_message(),
            });
        }
    };

    if !finish(true) {
        // Locked (screen lock, sleep, tray) while the prompt was up: that lock wins.
        if !session.token.is_empty()
            && let Err(e) = client.revoke_sessions().await
        {
            log::warn!("could not revoke the session of an aborted unlock: {e}");
        }
        return Err("Polytone was locked again. Try once more.".into());
    }
    audit(
        &app,
        "session.unlock",
        serde_json::json!({ "idle_timeout_s": session.idle_timeout_s }),
    );
    Ok(UnlockResult {
        token: session.token,
        idle_timeout_s: session.idle_timeout_s,
    })
}

/// Lock the UI. `reason`: "ui" (default), "idle", or "expired" (the UI saw a 401; nothing
/// to revoke).
#[tauri::command]
pub fn lock(app: AppHandle, reason: Option<String>) {
    lock_app(&app, LockReason::parse(reason.as_deref()));
}

#[tauri::command]
pub fn get_lock_state(app: AppHandle) -> LockStateDto {
    LockStateDto {
        locked: machine(&app).lock().unwrap().locked(),
    }
}

/// Wrapper kept in `AppState`.
#[derive(Debug, Default)]
pub struct LockCell {
    inner: Mutex<LockMachine>,
}

/// Lock on screen lock, sleep and user switch. Call from the main thread during setup.
pub fn watch_system_lock(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    {
        use crate::macos::session_events::{LockTrigger, observe};
        let app = app.clone();
        observe(move |t| {
            let reason = match t {
                LockTrigger::Sleep => LockReason::Sleep,
                LockTrigger::ScreenLocked | LockTrigger::SessionResigned => LockReason::Screen,
            };
            lock_app(&app, reason);
        });
    }
    #[cfg(not(target_os = "macos"))]
    let _ = app;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_is_43_base64url_chars_and_random() {
        let a = generate_token();
        let b = generate_token();
        assert_eq!(a.len(), 43);
        assert!(
            a.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        );
        assert_ne!(a, b);
        let raw = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(&a)
            .unwrap();
        assert_eq!(raw.len(), 32);
    }

    #[test]
    fn data_dir_resolution() {
        let repo = Path::new("/r");
        assert_eq!(
            resolve_data_dir(None, Some(repo)),
            Some(PathBuf::from("/r/data"))
        );
        assert_eq!(
            resolve_data_dir(Some("/abs/d".into()), Some(repo)),
            Some(PathBuf::from("/abs/d"))
        );
        assert_eq!(
            resolve_data_dir(Some("./data2".into()), Some(repo)),
            Some(PathBuf::from("/r/./data2"))
        );
        assert_eq!(
            resolve_data_dir(Some(" ".into()), Some(repo)),
            Some(PathBuf::from("/r/data"))
        );
        assert_eq!(
            resolve_data_dir(Some("/abs".into()), None),
            Some(PathBuf::from("/abs"))
        );
        assert_eq!(resolve_data_dir(None, None), None);
    }

    #[cfg(unix)]
    #[test]
    fn private_dir_and_log_modes() {
        use std::os::unix::fs::PermissionsExt;
        let base = std::env::temp_dir().join(format!("polytone-sec-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let dir = base.join("data");
        ensure_private_dir(&dir).unwrap();
        let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode(&dir), 0o700);
        let log = dir.join("backend.log");
        drop(open_private_append(&log).unwrap());
        assert_eq!(mode(&log), 0o600);
        // An existing, too-open log is tightened.
        std::fs::set_permissions(&log, std::fs::Permissions::from_mode(0o644)).unwrap();
        drop(open_private_append(&log).unwrap());
        assert_eq!(mode(&log), 0o600);
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn token_file_reading() {
        let p = std::env::temp_dir().join(format!("polytone-tok-{}", std::process::id()));
        assert_eq!(read_token_file(&p), None);
        std::fs::write(&p, "\n").unwrap();
        assert_eq!(read_token_file(&p), None);
        std::fs::write(&p, "abc_DEF-1\n").unwrap();
        assert_eq!(read_token_file(&p).as_deref(), Some("abc_DEF-1"));
        let _ = std::fs::remove_file(p);
    }

    #[test]
    fn lock_machine_starts_locked_and_unlocks() {
        let mut m = LockMachine::default();
        assert!(m.locked());
        let t = m.begin_unlock().unwrap();
        assert!(m.begin_unlock().is_none(), "one prompt at a time");
        assert!(m.locked());
        assert!(m.finish_unlock(t, true));
        assert!(!m.locked());
        assert!(m.lock());
        assert!(m.locked());
        assert!(!m.lock(), "locking twice changes nothing");
    }

    #[test]
    fn failed_unlock_stays_locked_and_allows_retry() {
        let mut m = LockMachine::default();
        let t = m.begin_unlock().unwrap();
        assert!(!m.finish_unlock(t, false));
        assert!(m.locked());
        let t = m.begin_unlock().unwrap();
        assert!(m.finish_unlock(t, true));
    }

    #[test]
    fn lock_during_prompt_wins() {
        let mut m = LockMachine::default();
        let t = m.begin_unlock().unwrap();
        assert!(m.lock(), "a pending unlock counts as a change");
        // Still one prompt at a time until the pending one returns.
        assert!(m.begin_unlock().is_none());
        assert!(!m.finish_unlock(t, true));
        assert!(m.locked());
        let t = m.begin_unlock().unwrap();
        assert!(m.finish_unlock(t, true));
        assert!(!m.locked());
    }

    #[test]
    fn lock_reasons() {
        assert_eq!(LockReason::parse(None), LockReason::Ui);
        assert_eq!(LockReason::parse(Some("idle")), LockReason::Idle);
        assert_eq!(LockReason::parse(Some("expired")), LockReason::Expired);
        assert_eq!(LockReason::parse(Some("bogus")), LockReason::Ui);
        assert_eq!(
            serde_json::to_value(LockedEvent {
                reason: LockReason::Sleep
            })
            .unwrap(),
            serde_json::json!({"reason":"sleep"})
        );
    }

    #[test]
    fn auth_errors_map_to_clear_messages() {
        assert_eq!(auth_error_message(la::USER_CANCEL), "Unlock cancelled.");
        assert!(auth_error_message(la::PASSCODE_NOT_SET).contains("login password"));
        assert!(auth_error_message(la::BIOMETRY_NOT_ENROLLED).contains("isn't set up"));
        assert!(auth_error_message(la::BIOMETRY_NOT_AVAILABLE).contains("Mac password"));
        assert!(auth_error_message(la::BIOMETRY_LOCKOUT).contains("Mac password"));
        assert_eq!(auth_error_message(-99), "Unlock failed (error -99).");
        assert_eq!(auth_error_kind(la::USER_CANCEL), "cancelled");
        assert_eq!(auth_error_kind(la::BIOMETRY_LOCKOUT), "lockout");
    }
}
