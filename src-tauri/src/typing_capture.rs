//! Typing capture: learn from messages the user types and sends in allowlisted apps
//! (CONTRACTS.md "Typing capture"). Opt-in, default OFF, local only.
//!
//! Pipeline:
//! 1. A listen-only `CGEventTap` (keyDown) on its own thread. The callback only re-enables the
//!    tap if macOS disabled it and forwards `(keycode, flags)` over a channel — it never blocks.
//! 2. A worker thread gates every key: capture enabled, hotkey not paused, secure input off,
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

use std::collections::HashMap;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use tauri::{AppHandle, Manager};

use crate::AppState;
use crate::backend_client::{BackendConfig, TypedIngestRequest};

pub const DEBOUNCE: Duration = Duration::from_millis(300);
pub const MIN_WORDS: usize = 3;
pub const MAX_CHARS: usize = 5000;
/// A send this soon after a Polytone inject in the same app is (an edit of) a draft.
pub const INJECT_WINDOW: Duration = Duration::from_secs(600);
/// After a send key: re-read the field at these offsets; a send clears it / moves focus.
pub const CONFIRM_DELAYS: [Duration; 2] = [Duration::from_millis(250), Duration::from_millis(900)];

pub const KC_RETURN: i64 = 36;
pub const KC_KEYPAD_ENTER: i64 = 76;
pub const KC_D: i64 = 2;

pub const FLAG_SHIFT: u64 = 0x0002_0000;
pub const FLAG_CONTROL: u64 = 0x0004_0000;
pub const FLAG_ALT: u64 = 0x0008_0000;
pub const FLAG_COMMAND: u64 = 0x0010_0000;

pub const MAIL_BUNDLE: &str = "com.apple.mail";
/// Apps where a bare Return inserts a newline rather than sending.
pub const RETURN_IS_NEWLINE: &[&str] = &["com.apple.mail", "com.apple.Notes", "notion.id"];

// ---------------------------------------------------------------------------
// Pure logic (unit-tested)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyAction {
    /// The user may be sending the message: capture it.
    Send,
    /// Anything else: the field text may have changed, re-read it (debounced).
    Edit,
}

/// Classify a keyDown in app `bundle_id`.
pub fn classify_key(keycode: i64, flags: u64, bundle_id: &str) -> KeyAction {
    let cmd = flags & FLAG_COMMAND != 0;
    let shift = flags & FLAG_SHIFT != 0;
    let ctrl = flags & FLAG_CONTROL != 0;
    let alt = flags & FLAG_ALT != 0;
    match keycode {
        KC_RETURN | KC_KEYPAD_ENTER => {
            let cmd_return = cmd && !ctrl;
            let bare_return =
                !cmd && !shift && !ctrl && !alt && !RETURN_IS_NEWLINE.contains(&bundle_id);
            if cmd_return || bare_return {
                KeyAction::Send
            } else {
                KeyAction::Edit
            }
        }
        KC_D if cmd && shift && !ctrl && !alt && bundle_id == MAIL_BUNDLE => KeyAction::Send,
        _ => KeyAction::Edit,
    }
}

pub fn word_count(text: &str) -> usize {
    text.split_whitespace()
        .filter(|w| w.chars().any(char::is_alphanumeric))
        .count()
}

/// Trim, drop items under [`MIN_WORDS`] words, cap at [`MAX_CHARS`] characters
/// (cut back to a word boundary when one is near).
pub fn prepare_text(text: &str) -> Option<String> {
    let t = text.trim();
    if word_count(t) < MIN_WORDS {
        return None;
    }
    if t.chars().count() <= MAX_CHARS {
        return Some(t.to_string());
    }
    let end = t.char_indices().nth(MAX_CHARS).map_or(t.len(), |(i, _)| i);
    let head = &t[..end];
    let cut = match head.rfind(char::is_whitespace) {
        Some(i) if i > end / 2 => &head[..i],
        _ => head,
    };
    Some(cut.trim_end().to_string())
}

/// Text to capture on a send key: the debounced cache, unless an immediate read of the
/// *same* element still has (non-empty) text — that one includes the last few keystrokes.
pub fn pick_text(
    cached: Option<&str>,
    immediate: Option<&str>,
    same_element: bool,
) -> Option<String> {
    let nonempty = |s: Option<&str>| s.filter(|v| !v.trim().is_empty()).map(str::to_string);
    match (nonempty(cached), nonempty(immediate)) {
        (_, Some(now)) if same_element => Some(now),
        (Some(c), _) => Some(c),
        (None, now) => now,
    }
}

/// Did the send key really send? Yes if focus left the element, the field became empty /
/// unreadable, or it no longer contains the text (it was replaced, not appended to).
pub fn confirm_sent(text: &str, same_element: bool, value_now: Option<&str>) -> bool {
    if !same_element {
        return true;
    }
    match value_now {
        None => true,
        Some(v) => v.trim().is_empty() || !v.contains(text.trim()),
    }
}

fn hash_text(text: &str) -> u64 {
    let mut h = DefaultHasher::new();
    text.trim().hash(&mut h);
    h.finish()
}

/// Last text sent per app, so an identical message isn't posted twice.
#[derive(Debug, Default)]
pub struct SentLog {
    last: HashMap<String, u64>,
}

impl SentLog {
    /// True (and remembered) if `text` differs from the last text sent for `app`.
    pub fn should_send(&mut self, app: &str, text: &str) -> bool {
        let h = hash_text(text);
        if self.last.get(app) == Some(&h) {
            return false;
        }
        self.last.insert(app.to_string(), h);
        true
    }
}

/// When/where Polytone last injected a draft.
#[derive(Debug, Default)]
pub struct InjectionTracker {
    last: Option<(String, Instant)>,
}

impl InjectionTracker {
    pub fn record(&mut self, app: &str, at: Instant) {
        self.last = Some((app.to_string(), at));
    }

    pub fn after_inject(&self, app: &str, now: Instant) -> bool {
        matches!(&self.last, Some((a, t)) if a == app && now.saturating_duration_since(*t) <= INJECT_WINDOW)
    }
}

/// RFC 3339 UTC timestamp (millisecond precision) without a date crate.
pub fn iso_utc(t: SystemTime) -> String {
    let d = t.duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = d.as_secs() as i64;
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // civil_from_days (Howard Hinnant)
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60,
        d.subsec_millis()
    )
}

// ---------------------------------------------------------------------------
// Shared state + Tauri commands
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
    /// Set by `stop()`; the tap thread's run loop checks it at least once a second.
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    stop_requested: AtomicBool,
    captured: AtomicU32,
    status: Mutex<StatusInner>,
    injections: Mutex<InjectionTracker>,
}

#[derive(Default)]
struct StatusInner {
    apps: Vec<String>,
    last_captured_at: Option<String>,
    last_error: Option<String>,
    /// `CFRunLoopRef` of the tap thread while it runs (as usize so it is Send).
    run_loop: usize,
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

fn allowlisted(bundle_id: &str) -> bool {
    shared()
        .status
        .lock()
        .unwrap()
        .apps
        .iter()
        .any(|a| a == bundle_id)
}

/// Apply `enabled` / `apps`: start the tap (retrying a failed start) or stop it.
pub fn apply(app: &AppHandle, enabled: bool, apps: Vec<String>) -> CaptureStatus {
    let _ = APP.set(app.clone());
    let s = shared();
    let mut cleaned: Vec<String> = Vec::new();
    for a in apps.into_iter().map(|a| a.trim().to_string()) {
        if !a.is_empty() && !cleaned.contains(&a) {
            cleaned.push(a);
        }
    }
    s.status.lock().unwrap().apps = cleaned;
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
    status()
}

/// Called from `sync_from_backend_config` with the backend's `/config`.
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
        shared().status.lock().unwrap().apps = apps;
    }
}

/// Record that Polytone just injected a draft into the frontmost app.
pub fn record_injection() {
    if let Some(front) = platform::frontmost_app() {
        shared()
            .injections
            .lock()
            .unwrap()
            .record(&front.bundle_id, Instant::now());
    }
}

fn after_inject(bundle_id: &str) -> bool {
    shared()
        .injections
        .lock()
        .unwrap()
        .after_inject(bundle_id, Instant::now())
}

fn paused() -> bool {
    APP.get()
        .is_some_and(|a| a.state::<AppState>().paused.load(Ordering::SeqCst))
}

/// Fire-and-forget `POST /ingest/typed`.
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

#[tauri::command]
pub fn set_capture(app: AppHandle, enabled: bool, apps: Vec<String>) -> CaptureStatus {
    apply(&app, enabled, apps)
}

#[tauri::command]
pub fn get_capture_status() -> CaptureStatus {
    status()
}

#[tauri::command]
pub fn list_running_apps() -> Vec<RunningApp> {
    platform::running_apps()
}

// ---------------------------------------------------------------------------
// macOS implementation
// ---------------------------------------------------------------------------

#[cfg(target_os = "macos")]
mod platform {
    use std::collections::{HashMap, HashSet};
    use std::ffi::c_void;
    use std::sync::atomic::{AtomicPtr, Ordering};
    use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
    use std::thread;
    use std::time::{Duration, Instant};

    use objc2::rc::autoreleasepool;
    use objc2_app_kit::{NSApplicationActivationPolicy, NSRunningApplication, NSWorkspace};

    use super::*;

    // ----------------------------------------------------------------- FFI
    type CFTypeRef = *const c_void;
    type CFStringRef = *const c_void;
    type AXUIElementRef = *const c_void;
    type CFMachPortRef = *mut c_void;
    type CFRunLoopRef = *mut c_void;
    type CFRunLoopSourceRef = *mut c_void;
    type CGEventRef = *mut c_void;
    type TapCallback = extern "C" fn(
        proxy: *mut c_void,
        ty: u32,
        event: CGEventRef,
        refcon: *mut c_void,
    ) -> CGEventRef;

    const UTF8: u32 = 0x0800_0100;
    const K_CG_SESSION_EVENT_TAP: u32 = 1;
    const K_CG_HEAD_INSERT: u32 = 0;
    const K_CG_LISTEN_ONLY: u32 = 1;
    const K_CG_EVENT_KEY_DOWN: u32 = 10;
    const K_CG_KEYBOARD_EVENT_KEYCODE: u32 = 9;
    const K_TAP_DISABLED_BY_TIMEOUT: u32 = 0xFFFF_FFFE;
    const K_TAP_DISABLED_BY_USER_INPUT: u32 = 0xFFFF_FFFF;

    #[link(name = "CoreFoundation", kind = "framework")]
    unsafe extern "C" {
        static kCFRunLoopCommonModes: CFStringRef;
        static kCFRunLoopDefaultMode: CFStringRef;
        static kCFBooleanTrue: CFTypeRef;
        fn CFRelease(cf: CFTypeRef);
        fn CFEqual(a: CFTypeRef, b: CFTypeRef) -> u8;
        fn CFGetTypeID(cf: CFTypeRef) -> usize;
        fn CFStringGetTypeID() -> usize;
        fn CFStringGetLength(s: CFStringRef) -> isize;
        fn CFStringGetMaximumSizeForEncoding(len: isize, enc: u32) -> isize;
        fn CFStringGetCString(s: CFStringRef, buf: *mut u8, size: isize, enc: u32) -> u8;
        fn CFStringCreateWithBytes(
            alloc: CFTypeRef,
            bytes: *const u8,
            len: isize,
            enc: u32,
            external: u8,
        ) -> CFStringRef;
        fn CFRunLoopGetCurrent() -> CFRunLoopRef;
        fn CFRunLoopAddSource(rl: CFRunLoopRef, src: CFRunLoopSourceRef, mode: CFStringRef);
        fn CFRunLoopRunInMode(mode: CFStringRef, seconds: f64, return_after_source: u8) -> i32;
        fn CFRunLoopStop(rl: CFRunLoopRef);
        fn CFMachPortCreateRunLoopSource(
            alloc: CFTypeRef,
            port: CFMachPortRef,
            order: isize,
        ) -> CFRunLoopSourceRef;
        fn CFMachPortInvalidate(port: CFMachPortRef);
    }

    #[link(name = "ApplicationServices", kind = "framework")]
    unsafe extern "C" {
        fn CGEventTapCreate(
            tap: u32,
            place: u32,
            options: u32,
            events: u64,
            callback: TapCallback,
            refcon: *mut c_void,
        ) -> CFMachPortRef;
        fn CGEventTapEnable(tap: CFMachPortRef, enable: bool);
        fn CGEventGetIntegerValueField(event: CGEventRef, field: u32) -> i64;
        fn CGEventGetFlags(event: CGEventRef) -> u64;
        fn CGPreflightListenEventAccess() -> bool;
        fn CGRequestListenEventAccess() -> bool;
        fn AXUIElementCreateSystemWide() -> AXUIElementRef;
        fn AXUIElementCreateApplication(pid: i32) -> AXUIElementRef;
        fn AXUIElementCopyAttributeValue(
            el: AXUIElementRef,
            attr: CFStringRef,
            out: *mut CFTypeRef,
        ) -> i32;
        fn AXUIElementSetAttributeValue(
            el: AXUIElementRef,
            attr: CFStringRef,
            value: CFTypeRef,
        ) -> i32;
        fn AXUIElementGetPid(el: AXUIElementRef, pid: *mut i32) -> i32;
        fn AXUIElementSetMessagingTimeout(el: AXUIElementRef, secs: f32) -> i32;
    }

    #[link(name = "Carbon", kind = "framework")]
    unsafe extern "C" {
        fn IsSecureEventInputEnabled() -> u8;
    }

    /// Owned CF object, released on drop.
    struct Cf(CFTypeRef);

    impl Cf {
        fn new(p: CFTypeRef) -> Option<Self> {
            (!p.is_null()).then_some(Cf(p))
        }
    }

    impl Drop for Cf {
        fn drop(&mut self) {
            // SAFETY: we own one retain on a non-null CF object.
            unsafe { CFRelease(self.0) }
        }
    }

    fn cfstr(s: &str) -> Cf {
        // SAFETY: valid UTF-8 bytes + length; returns a +1 CFString.
        Cf(unsafe {
            CFStringCreateWithBytes(std::ptr::null(), s.as_ptr(), s.len() as isize, UTF8, 0)
        })
    }

    fn cf_to_string(p: CFTypeRef) -> Option<String> {
        // SAFETY: p is a live CF object; type-checked before use as a CFString.
        unsafe {
            if p.is_null() || CFGetTypeID(p) != CFStringGetTypeID() {
                return None;
            }
            let len = CFStringGetLength(p);
            let cap = CFStringGetMaximumSizeForEncoding(len, UTF8) + 1;
            let mut buf = vec![0u8; cap.max(1) as usize];
            if CFStringGetCString(p, buf.as_mut_ptr(), cap, UTF8) == 0 {
                return None;
            }
            let n = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
            buf.truncate(n);
            String::from_utf8(buf).ok()
        }
    }

    fn ax_attr(el: AXUIElementRef, name: &str) -> Option<Cf> {
        let attr = cfstr(name);
        let mut out: CFTypeRef = std::ptr::null();
        // SAFETY: el is a live AXUIElement, out receives a +1 reference on success.
        let err = unsafe { AXUIElementCopyAttributeValue(el, attr.0, &mut out) };
        if err == 0 { Cf::new(out) } else { None }
    }

    fn ax_string(el: AXUIElementRef, name: &str) -> Option<String> {
        ax_attr(el, name).and_then(|v| cf_to_string(v.0))
    }

    fn same(a: &Cf, b: &Cf) -> bool {
        // SAFETY: both are live CF objects.
        unsafe { CFEqual(a.0, b.0) != 0 }
    }

    pub fn secure_input() -> bool {
        // SAFETY: no arguments.
        unsafe { IsSecureEventInputEnabled() != 0 }
    }

    // ----------------------------------------------------------------- apps
    pub struct Front {
        pub bundle_id: String,
        pub name: Option<String>,
        pub pid: i32,
    }

    /// pid of the app that receives keystrokes, per Accessibility (None without permission).
    fn ax_focused_pid() -> Option<i32> {
        // SAFETY: +1 system-wide element; el is live while we read its pid.
        let sys = Cf::new(unsafe { AXUIElementCreateSystemWide() })?;
        unsafe { AXUIElementSetMessagingTimeout(sys.0, 0.25) };
        let el = ax_attr(sys.0, "AXFocusedApplication")?;
        let mut pid = 0i32;
        (unsafe { AXUIElementGetPid(el.0, &mut pid) } == 0 && pid > 0).then_some(pid)
    }

    /// The app keystrokes go to: the Accessibility focused application (always current, even
    /// off the main thread), falling back to `NSWorkspace.frontmostApplication`.
    pub fn frontmost_app() -> Option<Front> {
        autoreleasepool(|_| {
            let app = ax_focused_pid()
                .and_then(NSRunningApplication::runningApplicationWithProcessIdentifier)
                .or_else(|| NSWorkspace::sharedWorkspace().frontmostApplication())?;
            Some(Front {
                bundle_id: app.bundleIdentifier()?.to_string(),
                name: app.localizedName().map(|n| n.to_string()),
                pid: app.processIdentifier(),
            })
        })
    }

    pub fn running_apps() -> Vec<RunningApp> {
        autoreleasepool(|_| {
            let ws = NSWorkspace::sharedWorkspace();
            let mut out: Vec<RunningApp> = Vec::new();
            for app in ws.runningApplications().iter() {
                let app: &NSRunningApplication = &app;
                if app.activationPolicy() != NSApplicationActivationPolicy::Regular {
                    continue;
                }
                let Some(bid) = app.bundleIdentifier().map(|b| b.to_string()) else {
                    continue;
                };
                if out.iter().any(|a| a.bundle_id == bid) {
                    continue;
                }
                let name = app
                    .localizedName()
                    .map_or_else(|| bid.clone(), |n| n.to_string());
                out.push(RunningApp {
                    bundle_id: bid,
                    name,
                });
            }
            out.sort_by_key(|a| a.name.to_lowercase());
            out
        })
    }

    // ----------------------------------------------------------------- tap thread
    enum Msg {
        Key { keycode: i64, flags: u64 },
    }

    struct TapCtx {
        tx: Sender<Msg>,
        port: AtomicPtr<c_void>,
    }

    extern "C" fn tap_callback(
        _proxy: *mut c_void,
        ty: u32,
        event: CGEventRef,
        refcon: *mut c_void,
    ) -> CGEventRef {
        if refcon.is_null() {
            return event;
        }
        // SAFETY: refcon is the TapCtx owned by the tap thread, alive while the tap exists.
        let ctx = unsafe { &*(refcon as *const TapCtx) };
        if ty == K_TAP_DISABLED_BY_TIMEOUT || ty == K_TAP_DISABLED_BY_USER_INPUT {
            let port = ctx.port.load(Ordering::SeqCst);
            if !port.is_null() {
                // SAFETY: port is our live event tap.
                unsafe { CGEventTapEnable(port, true) };
            }
            return event;
        }
        if ty == K_CG_EVENT_KEY_DOWN && !event.is_null() && shared().enabled.load(Ordering::Relaxed)
        {
            // SAFETY: event is the live CGEvent handed to the callback.
            let (keycode, flags) = unsafe {
                (
                    CGEventGetIntegerValueField(event, K_CG_KEYBOARD_EVENT_KEYCODE),
                    CGEventGetFlags(event),
                )
            };
            let _ = ctx.tx.send(Msg::Key { keycode, flags }); // unbounded: never blocks
        }
        event
    }

    /// Serializes start/stop so two `set_capture` calls can't create two taps.
    static LIFECYCLE: Mutex<()> = Mutex::new(());

    pub fn start() -> Result<(), String> {
        let _guard = LIFECYCLE.lock().unwrap();
        let s = shared();
        if s.running.load(Ordering::SeqCst) {
            return Ok(());
        }
        s.stop_requested.store(false, Ordering::SeqCst);
        // SAFETY: plain status queries / prompt request.
        if !unsafe { CGPreflightListenEventAccess() } {
            unsafe { CGRequestListenEventAccess() };
        }
        let (tx, rx) = mpsc::channel::<Msg>();
        let (ready_tx, ready_rx) = mpsc::channel::<Result<(), String>>();
        thread::Builder::new()
            .name("polytone-capture-worker".into())
            .spawn(move || Worker::default().run(rx))
            .map_err(|e| e.to_string())?;
        thread::Builder::new()
            .name("polytone-capture-tap".into())
            .spawn(move || tap_thread(tx, ready_tx))
            .map_err(|e| e.to_string())?;
        match ready_rx.recv_timeout(Duration::from_secs(2)) {
            Ok(r) => r,
            Err(_) => Err("event tap did not start in time".into()),
        }
    }

    fn tap_thread(tx: Sender<Msg>, ready: Sender<Result<(), String>>) {
        let ctx = Box::new(TapCtx {
            tx,
            port: AtomicPtr::new(std::ptr::null_mut()),
        });
        let ctx_ptr = Box::into_raw(ctx);
        // SAFETY: ctx_ptr stays valid until we free it after the run loop exits.
        let port = unsafe {
            CGEventTapCreate(
                K_CG_SESSION_EVENT_TAP,
                K_CG_HEAD_INSERT,
                K_CG_LISTEN_ONLY,
                1u64 << K_CG_EVENT_KEY_DOWN,
                tap_callback,
                ctx_ptr.cast(),
            )
        };
        if port.is_null() {
            // SAFETY: the tap was never created, so nothing else references ctx.
            drop(unsafe { Box::from_raw(ctx_ptr) });
            let _ = ready.send(Err(
                "could not listen to the keyboard: grant Polytone Input Monitoring and \
                 Accessibility (System Settings > Privacy & Security), then turn capture on again"
                    .into(),
            ));
            return;
        }
        // SAFETY: port is a live event tap; the run loop calls are made on this thread.
        unsafe {
            (*ctx_ptr).port.store(port, Ordering::SeqCst);
            let src = CFMachPortCreateRunLoopSource(std::ptr::null(), port, 0);
            let rl = CFRunLoopGetCurrent();
            CFRunLoopAddSource(rl, src, kCFRunLoopCommonModes);
            CGEventTapEnable(port, true);
            shared().status.lock().unwrap().run_loop = rl as usize;
            shared().running.store(true, Ordering::SeqCst);
            let _ = ready.send(Ok(()));
            log::info!("typing capture: keyboard tap running");
            // `stop()` sets the flag and wakes us; the 1s slice covers a stop that raced startup.
            while !shared().stop_requested.load(Ordering::SeqCst) {
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, 0);
            }
            shared().running.store(false, Ordering::SeqCst);
            shared().status.lock().unwrap().run_loop = 0;
            CGEventTapEnable(port, false);
            CFMachPortInvalidate(port);
            CFRelease(src as CFTypeRef);
            CFRelease(port as CFTypeRef);
            drop(Box::from_raw(ctx_ptr)); // drops tx → the worker exits
        }
        log::info!("typing capture: keyboard tap stopped");
    }

    /// Stop the tap and wait (≤ 2s) for its thread to tear it down.
    pub fn stop() {
        let _guard = LIFECYCLE.lock().unwrap();
        let s = shared();
        s.stop_requested.store(true, Ordering::SeqCst);
        let rl = s.status.lock().unwrap().run_loop;
        if rl != 0 {
            // SAFETY: CFRunLoopStop is thread-safe; rl belongs to the live tap thread.
            unsafe { CFRunLoopStop(rl as CFRunLoopRef) };
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        while s.running.load(Ordering::SeqCst) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
    }

    // ----------------------------------------------------------------- worker
    struct Focused {
        element: Cf,
        value: Option<String>,
    }

    /// Read the focused text element of `front` (None for password fields / non-text / other pid).
    fn read_focused(front: &Front) -> Option<Focused> {
        // SAFETY: returns a +1 system-wide element.
        let sys = Cf::new(unsafe { AXUIElementCreateSystemWide() })?;
        // SAFETY: live element; bound how long a hung app can stall us.
        unsafe { AXUIElementSetMessagingTimeout(sys.0, 0.25) };
        let el = ax_attr(sys.0, "AXFocusedUIElement")?;
        let mut pid = 0i32;
        // SAFETY: el is a live AXUIElement.
        if unsafe { AXUIElementGetPid(el.0, &mut pid) } != 0 || pid != front.pid {
            return None;
        }
        let subrole = ax_string(el.0, "AXSubrole").unwrap_or_default();
        if subrole == "AXSecureTextField" || subrole == "AXSearchField" {
            return None;
        }
        let role = ax_string(el.0, "AXRole").unwrap_or_default();
        if !matches!(role.as_str(), "AXTextArea" | "AXTextField" | "AXComboBox") {
            return None;
        }
        let value = ax_string(el.0, "AXValue");
        Some(Focused { element: el, value })
    }

    /// Electron / Chromium apps only build their AX tree for assistive clients that ask.
    fn prepare_app(pid: i32) {
        // SAFETY: +1 application element; kCFBooleanTrue is a static CF constant.
        unsafe {
            if let Some(app) = Cf::new(AXUIElementCreateApplication(pid)) {
                let attr = cfstr("AXManualAccessibility");
                AXUIElementSetAttributeValue(app.0, attr.0, kCFBooleanTrue);
            }
        }
    }

    struct Cached {
        text: String,
        element: Cf,
    }

    struct Pending {
        front_bundle: String,
        app_name: Option<String>,
        pid: i32,
        element: Cf,
        text: String,
        after_inject: bool,
        started: Instant,
        check: usize,
    }

    impl Pending {
        fn due(&self) -> Instant {
            self.started + CONFIRM_DELAYS[self.check]
        }
    }

    #[derive(Default)]
    struct Worker {
        cache: HashMap<String, Cached>,
        read_due: Option<Instant>,
        pending: Vec<Pending>,
        sent: SentLog,
        prepared: HashSet<i32>,
    }

    impl Worker {
        fn run(mut self, rx: Receiver<Msg>) {
            loop {
                let next = self
                    .pending
                    .iter()
                    .map(Pending::due)
                    .chain(self.read_due)
                    .min();
                let wait = next.map_or(Duration::from_secs(3600), |t| {
                    t.saturating_duration_since(Instant::now())
                });
                match rx.recv_timeout(wait) {
                    Ok(Msg::Key { keycode, flags }) => {
                        autoreleasepool(|_| self.on_key(keycode, flags))
                    }
                    Err(RecvTimeoutError::Timeout) => {}
                    Err(RecvTimeoutError::Disconnected) => break,
                }
                autoreleasepool(|_| self.tick());
            }
        }

        /// Gate: capture on, not paused, no secure input, frontmost app allowlisted.
        fn gate(&mut self) -> Option<Front> {
            if !shared().enabled.load(Ordering::SeqCst) || paused() {
                return None;
            }
            if secure_input() {
                self.cache.clear();
                return None;
            }
            let front = frontmost_app()?;
            if !allowlisted(&front.bundle_id) {
                return None;
            }
            if self.prepared.insert(front.pid) {
                prepare_app(front.pid);
            }
            Some(front)
        }

        fn on_key(&mut self, keycode: i64, flags: u64) {
            let Some(front) = self.gate() else { return };
            match classify_key(keycode, flags, &front.bundle_id) {
                KeyAction::Edit => self.read_due = Some(Instant::now() + DEBOUNCE),
                KeyAction::Send => self.on_send(front),
            }
        }

        fn refresh_cache(&mut self) {
            let Some(front) = self.gate() else { return };
            match read_focused(&front) {
                Some(Focused {
                    element,
                    value: Some(text),
                }) => {
                    self.cache.insert(front.bundle_id, Cached { text, element });
                }
                // password / search field / unreadable: forget what we had for this app
                _ => {
                    self.cache.remove(&front.bundle_id);
                }
            }
        }

        fn on_send(&mut self, front: Front) {
            self.read_due = None;
            let cached = self.cache.remove(&front.bundle_id);
            let now = read_focused(&front);
            let same_el = match (&cached, &now) {
                (Some(c), Some(n)) => same(&c.element, &n.element),
                (None, Some(_)) => true,
                _ => false,
            };
            let text = pick_text(
                cached.as_ref().map(|c| c.text.as_str()),
                now.as_ref().and_then(|n| n.value.as_deref()),
                same_el,
            );
            let Some(text) = text.as_deref().and_then(prepare_text) else {
                return;
            };
            // the element the text lives in: confirm the send by watching it clear
            let element = match (cached, now) {
                (_, Some(n)) if same_el => n.element,
                (Some(c), _) => c.element,
                (None, Some(n)) => n.element,
                (None, None) => return,
            };
            self.pending.push(Pending {
                after_inject: after_inject(&front.bundle_id),
                front_bundle: front.bundle_id,
                app_name: front.name,
                pid: front.pid,
                element,
                text,
                started: Instant::now(),
                check: 0,
            });
        }

        fn tick(&mut self) {
            let now = Instant::now();
            if self.read_due.is_some_and(|t| t <= now) {
                self.read_due = None;
                self.refresh_cache();
            }
            let mut i = 0;
            while i < self.pending.len() {
                if self.pending[i].due() > now {
                    i += 1;
                    continue;
                }
                let mut p = self.pending.swap_remove(i);
                let front = Front {
                    bundle_id: p.front_bundle.clone(),
                    name: None,
                    pid: p.pid,
                };
                let focused = read_focused(&front);
                let same_el = focused
                    .as_ref()
                    .is_some_and(|f| same(&f.element, &p.element));
                let value = focused.as_ref().and_then(|f| f.value.as_deref());
                if confirm_sent(&p.text, same_el, value) {
                    self.send(p);
                } else if p.check + 1 < CONFIRM_DELAYS.len() {
                    p.check += 1;
                    self.pending.push(p);
                } else {
                    log::debug!(
                        "typing capture: Return in {} did not send; ignored",
                        p.front_bundle
                    );
                }
            }
        }

        fn send(&mut self, p: Pending) {
            if !shared().enabled.load(Ordering::SeqCst) || paused() {
                return;
            }
            if !self.sent.should_send(&p.front_bundle, &p.text) {
                return;
            }
            dispatch(TypedIngestRequest {
                text: p.text,
                app: p.front_bundle,
                app_name: p.app_name,
                after_inject: p.after_inject,
            });
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use super::RunningApp;

    pub struct Front {
        pub bundle_id: String,
    }

    pub fn start() -> Result<(), String> {
        Err("typing capture is only available on macOS".into())
    }

    pub fn stop() {}

    pub fn frontmost_app() -> Option<Front> {
        None
    }

    pub fn running_apps() -> Vec<RunningApp> {
        Vec::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SLACK: &str = "com.tinyspeck.slackmacgap";

    #[test]
    fn return_sends_in_chat_apps_only_without_shift() {
        assert_eq!(classify_key(KC_RETURN, 0, SLACK), KeyAction::Send);
        assert_eq!(classify_key(KC_KEYPAD_ENTER, 0, SLACK), KeyAction::Send);
        assert_eq!(classify_key(KC_RETURN, FLAG_SHIFT, SLACK), KeyAction::Edit);
        assert_eq!(classify_key(KC_RETURN, FLAG_ALT, SLACK), KeyAction::Edit);
        assert_eq!(
            classify_key(KC_RETURN, FLAG_CONTROL, SLACK),
            KeyAction::Edit
        );
        // a bare Return is a newline in Mail / Notes / Notion
        assert_eq!(classify_key(KC_RETURN, 0, MAIL_BUNDLE), KeyAction::Edit);
        assert_eq!(
            classify_key(KC_RETURN, 0, "com.apple.Notes"),
            KeyAction::Edit
        );
        // unrelated flag bits (caps lock 0x10000, non-coalesced 0x100) don't matter
        assert_eq!(classify_key(KC_RETURN, 0x0001_0100, SLACK), KeyAction::Send);
    }

    #[test]
    fn cmd_return_sends_everywhere() {
        assert_eq!(
            classify_key(KC_RETURN, FLAG_COMMAND, SLACK),
            KeyAction::Send
        );
        assert_eq!(
            classify_key(KC_RETURN, FLAG_COMMAND, MAIL_BUNDLE),
            KeyAction::Send
        );
        assert_eq!(
            classify_key(KC_RETURN, FLAG_COMMAND | FLAG_SHIFT, "com.apple.Notes"),
            KeyAction::Send
        );
        assert_eq!(
            classify_key(KC_RETURN, FLAG_COMMAND | FLAG_CONTROL, SLACK),
            KeyAction::Edit
        );
    }

    #[test]
    fn cmd_shift_d_sends_in_mail_only() {
        assert_eq!(
            classify_key(KC_D, FLAG_COMMAND | FLAG_SHIFT, MAIL_BUNDLE),
            KeyAction::Send
        );
        assert_eq!(
            classify_key(KC_D, FLAG_COMMAND | FLAG_SHIFT, "com.google.Chrome"),
            KeyAction::Edit
        );
        assert_eq!(
            classify_key(KC_D, FLAG_COMMAND, MAIL_BUNDLE),
            KeyAction::Edit
        );
        assert_eq!(classify_key(KC_D, 0, MAIL_BUNDLE), KeyAction::Edit);
        assert_eq!(classify_key(0, 0, SLACK), KeyAction::Edit); // 'a'
    }

    #[test]
    fn word_count_filter_and_cap() {
        assert_eq!(prepare_text("  ok sure  "), None);
        assert_eq!(prepare_text("ok — sure ?"), None); // punctuation isn't a word
        assert_eq!(
            prepare_text(" sounds good to me \n").as_deref(),
            Some("sounds good to me")
        );
        let long = "word ".repeat(2000);
        let out = prepare_text(&long).unwrap();
        assert!(out.chars().count() <= MAX_CHARS && out.ends_with("word"));
        let wide = format!("a b c {}", "é".repeat(6000)); // multibyte, no late whitespace
        assert_eq!(prepare_text(&wide).unwrap().chars().count(), MAX_CHARS);
    }

    #[test]
    fn dedupe_per_app() {
        let mut log = SentLog::default();
        assert!(log.should_send(SLACK, "see you at noon then"));
        assert!(!log.should_send(SLACK, "see you at noon then "));
        assert!(log.should_send("com.apple.MobileSMS", "see you at noon then"));
        assert!(log.should_send(SLACK, "something else entirely"));
        assert!(log.should_send(SLACK, "see you at noon then"));
    }

    #[test]
    fn after_inject_window() {
        let mut t = InjectionTracker::default();
        let t0 = Instant::now();
        assert!(!t.after_inject(SLACK, t0));
        t.record(SLACK, t0);
        assert!(t.after_inject(SLACK, t0 + Duration::from_secs(5)));
        assert!(t.after_inject(SLACK, t0 + INJECT_WINDOW));
        assert!(!t.after_inject(SLACK, t0 + INJECT_WINDOW + Duration::from_secs(1)));
        assert!(!t.after_inject(MAIL_BUNDLE, t0 + Duration::from_secs(5)));
        t.record(MAIL_BUNDLE, t0 + Duration::from_secs(10));
        assert!(!t.after_inject(SLACK, t0 + Duration::from_secs(11))); // only the last inject counts
    }

    #[test]
    fn pick_prefers_fresh_read_of_same_field() {
        assert_eq!(
            pick_text(Some("hello the"), Some("hello there"), true).as_deref(),
            Some("hello there")
        );
        // field already cleared by the send → cached text
        assert_eq!(
            pick_text(Some("hello there"), Some(""), true).as_deref(),
            Some("hello there")
        );
        // focus moved (Mail window closed) → cached text
        assert_eq!(
            pick_text(Some("hello there"), Some("inbox"), false).as_deref(),
            Some("hello there")
        );
        assert_eq!(
            pick_text(None, Some("typed fast"), true).as_deref(),
            Some("typed fast")
        );
        assert_eq!(pick_text(None, None, false), None);
    }

    #[test]
    fn confirm_send_needs_the_field_to_clear() {
        assert!(confirm_sent("hi there you", true, Some("")));
        assert!(confirm_sent("hi there you", true, None));
        assert!(confirm_sent("hi there you", false, Some("hi there you")));
        assert!(!confirm_sent("hi there you", true, Some("hi there you\n")));
        assert!(confirm_sent("hi there you", true, Some("new message")));
    }

    #[test]
    fn iso_format() {
        assert_eq!(iso_utc(UNIX_EPOCH), "1970-01-01T00:00:00.000Z");
        let t = UNIX_EPOCH + Duration::from_millis(1_793_356_245_123); // 2026-10-30
        assert_eq!(iso_utc(t), "2026-10-30T10:30:45.123Z");
        let leap = UNIX_EPOCH + Duration::from_secs(951_782_400); // 2000-02-29
        assert_eq!(iso_utc(leap), "2000-02-29T00:00:00.000Z");
    }

    /// Manual: `cargo test -- --ignored --nocapture macos_smoke` (needs a GUI session; the tap
    /// part needs Input Monitoring / Accessibility for the binary running the test).
    #[test]
    #[ignore]
    fn macos_smoke() {
        let apps = platform::running_apps();
        println!("running apps: {}", apps.len());
        assert!(!apps.is_empty());
        println!(
            "frontmost: {:?}",
            platform::frontmost_app().map(|f| f.bundle_id)
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

    #[test]
    fn status_defaults_off() {
        let s = status();
        assert!(!s.running);
        assert_eq!(s.captured_this_session, 0);
    }
}
