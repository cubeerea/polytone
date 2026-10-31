//! Global hotkey + the capture -> POST /generate -> inject loop
//! (CONTRACTS.md "Hotkey loop").
//!
//! The pure decision logic (`capture`, `inject`) is written against the
//! [`ClipboardIo`] and [`KeySim`] traits so it can be unit-tested with fakes.
//! The real implementations use `arboard` and `enigo`.
//!
//! Clipboard restore: before touching the clipboard we take a [`SavedClipboard`]
//! snapshot and put it back afterwards. On macOS that snapshot is every
//! NSPasteboard item with every representation as raw bytes (plain + rich text,
//! images, file URLs, app-private types), so images and copied files survive a
//! run. Elsewhere — or if the macOS snapshot is unavailable/too large — it falls
//! back to text, then an arboard RGBA image. Anything still unreadable is
//! [`SavedClipboard::Unrestorable`]: we then leave the clipboard as-is (holding the
//! draft) instead of wiping it. Known limits: lazily-promised data (file promises)
//! is only kept if the source app hands it over when asked, and the snapshot is
//! capped at [`MAX_SNAPSHOT_BYTES`].

use std::sync::atomic::Ordering;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutEvent, ShortcutState};

use crate::backend_client::{self, GenerateRequest, InjectMode};
use crate::{AppState, notify};

pub const DEFAULT_HOTKEY: &str = "CmdOrCtrl+Shift+P";
pub const EVENT_STARTED: &str = "generation-started";
pub const EVENT_FINISHED: &str = "generation-finished";

// ---------------------------------------------------------------------------
// Abstractions
// ---------------------------------------------------------------------------

/// Marker contained in every sentinel (see [`make_sentinel`]).
pub const SENTINEL_MARK: &str = "polytone-sentinel";
/// Larger native snapshots are skipped (we fall back to text / image).
pub const MAX_SNAPSHOT_BYTES: usize = 64 * 1024 * 1024;

/// RGBA8 image, as arboard reads/writes it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageSnapshot {
    pub width: usize,
    pub height: usize,
    pub rgba: Vec<u8>,
}

/// One pasteboard item: every `(type identifier, raw bytes)` representation it offered.
pub type NativeItem = Vec<(String, Vec<u8>)>;

/// Everything we need to put the user's clipboard back the way it was.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum SavedClipboard {
    #[default]
    Empty,
    Text(String),
    Image(ImageSnapshot),
    /// Full macOS pasteboard snapshot (all items, all types).
    Native(Vec<NativeItem>),
    /// Something is on the clipboard but we could not read it; never clobber it with `clear`.
    Unrestorable,
}

pub const PLAIN_TEXT_UTI: &str = "public.utf8-plain-text";

impl SavedClipboard {
    /// Plain-text view of the saved content (used as the step-3 capture fallback).
    pub fn text(&self) -> Option<String> {
        match self {
            SavedClipboard::Text(t) => Some(t.clone()),
            SavedClipboard::Native(items) => items
                .iter()
                .flatten()
                .find(|(t, _)| t == PLAIN_TEXT_UTI)
                .and_then(|(_, d)| String::from_utf8(d.clone()).ok())
                .filter(|t| !t.is_empty()),
            _ => None,
        }
    }
}

pub trait ClipboardIo {
    /// Current clipboard text, `None` if empty / not text / unreadable.
    fn get_text(&mut self) -> Option<String>;
    fn set_text(&mut self, text: &str) -> Result<(), String>;
    fn clear(&mut self) -> Result<(), String>;
    /// Current clipboard image, `None` if there is none.
    fn get_image(&mut self) -> Option<ImageSnapshot> {
        None
    }
    fn set_image(&mut self, _img: &ImageSnapshot) -> Result<(), String> {
        Err("restoring images is not supported here".into())
    }
    /// Full-fidelity snapshot of every item/type (macOS). `None` = unsupported / unavailable.
    fn save_native(&mut self) -> Option<Vec<NativeItem>> {
        None
    }
    fn restore_native(&mut self, _items: &[NativeItem]) -> Result<(), String> {
        Err("native clipboard restore is not supported here".into())
    }
    /// Whether the clipboard holds anything at all, of any type.
    fn has_content(&mut self) -> bool {
        false
    }
}

/// Snapshot the clipboard before the loop overwrites it.
pub fn save_clipboard<C: ClipboardIo>(clip: &mut C) -> SavedClipboard {
    if let Some(items) = clip.save_native().filter(|i| !i.is_empty()) {
        return SavedClipboard::Native(items);
    }
    if let Some(t) = clip.get_text() {
        return SavedClipboard::Text(t);
    }
    if let Some(img) = clip.get_image() {
        return SavedClipboard::Image(img);
    }
    if clip.has_content() {
        SavedClipboard::Unrestorable
    } else {
        SavedClipboard::Empty
    }
}

pub trait KeySim {
    /// Cmd+C (Ctrl+C off macOS).
    fn copy(&mut self) -> Result<(), String>;
    /// Cmd+A.
    fn select_all(&mut self) -> Result<(), String>;
    /// Cmd+V.
    fn paste(&mut self) -> Result<(), String>;
    /// Type characters directly.
    fn type_text(&mut self, text: &str) -> Result<(), String>;
}

#[derive(Debug, Clone, Copy)]
pub struct Timing {
    /// Pause between simulated keystrokes and clipboard operations.
    pub key_delay: Duration,
    /// How many times to poll the clipboard (spaced by `key_delay`) after Cmd+C.
    pub copy_polls: u32,
    /// Wait after Cmd+V before restoring the original clipboard.
    pub restore_delay: Duration,
}

impl Timing {
    pub const MACOS: Timing = Timing {
        key_delay: Duration::from_millis(80),
        copy_polls: 3,
        restore_delay: Duration::from_millis(300),
    };

    #[cfg(test)]
    pub const INSTANT: Timing = Timing {
        key_delay: Duration::ZERO,
        copy_polls: 1,
        restore_delay: Duration::ZERO,
    };

    fn pause(&self) {
        if !self.key_delay.is_zero() {
            thread::sleep(self.key_delay);
        }
    }
}

// ---------------------------------------------------------------------------
// Pure loop logic
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum CaptureSource {
    /// The user had text selected; Cmd+C picked it up.
    Selection,
    /// Nothing was selected; Cmd+A, Cmd+C grabbed the whole field.
    Field,
    /// Field was empty; fell back to what was on the clipboard before.
    Clipboard,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CaptureOutcome {
    Captured { text: String, source: CaptureSource },
    Empty,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureResult {
    pub outcome: CaptureOutcome,
    /// Clipboard text before we touched it (the step-3 fallback).
    pub original: Option<String>,
    /// Full clipboard content before we touched it; restore with [`restore_clipboard`].
    pub saved: SavedClipboard,
}

/// A clipboard value is usable intent if it is non-blank and not our sentinel.
pub fn usable(sentinel: &str, value: Option<&str>) -> Option<String> {
    match value {
        Some(v) if v != sentinel && !v.trim().is_empty() => Some(v.trim().to_string()),
        _ => None,
    }
}

/// A sentinel string that no real clipboard is going to contain.
pub fn make_sentinel() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("\u{2063}{SENTINEL_MARK}-{nanos}\u{2063}")
}

fn poll_clipboard<C: ClipboardIo>(clip: &mut C, sentinel: &str, timing: &Timing) -> Option<String> {
    let polls = timing.copy_polls.max(1);
    for i in 0..polls {
        if i > 0 {
            timing.pause();
        }
        if let Some(v) = usable(sentinel, clip.get_text().as_deref()) {
            return Some(v);
        }
    }
    None
}

/// Step 2 of the contract: selection -> whole field -> original clipboard -> empty.
///
/// On error the original clipboard is restored before returning.
pub fn capture<C: ClipboardIo, K: KeySim>(
    clip: &mut C,
    keys: &mut K,
    sentinel: &str,
    timing: &Timing,
) -> Result<CaptureResult, String> {
    let saved = save_clipboard(clip);
    let original = saved.text();
    let result = capture_inner(clip, keys, sentinel, timing, original.as_deref());
    match result {
        Ok(outcome) => Ok(CaptureResult {
            outcome,
            original,
            saved,
        }),
        Err(e) => {
            let _ = restore_clipboard(clip, &saved);
            Err(e)
        }
    }
}

fn capture_inner<C: ClipboardIo, K: KeySim>(
    clip: &mut C,
    keys: &mut K,
    sentinel: &str,
    timing: &Timing,
    original: Option<&str>,
) -> Result<CaptureOutcome, String> {
    clip.set_text(sentinel)?;
    timing.pause();

    // 1. Selection.
    keys.copy()?;
    timing.pause();
    if let Some(text) = poll_clipboard(clip, sentinel, timing) {
        return Ok(CaptureOutcome::Captured {
            text,
            source: CaptureSource::Selection,
        });
    }

    // 2. Whole field.
    keys.select_all()?;
    timing.pause();
    keys.copy()?;
    timing.pause();
    if let Some(text) = poll_clipboard(clip, sentinel, timing) {
        return Ok(CaptureOutcome::Captured {
            text,
            source: CaptureSource::Field,
        });
    }

    // 3. Original clipboard.
    if let Some(text) = usable(sentinel, original) {
        return Ok(CaptureOutcome::Captured {
            text,
            source: CaptureSource::Clipboard,
        });
    }

    Ok(CaptureOutcome::Empty)
}

/// Put the saved clipboard back. Falls back to plain text if a native restore fails;
/// for [`SavedClipboard::Unrestorable`] it only removes our own sentinel and otherwise
/// leaves the clipboard untouched.
pub fn restore_clipboard<C: ClipboardIo>(
    clip: &mut C,
    saved: &SavedClipboard,
) -> Result<(), String> {
    match saved {
        SavedClipboard::Empty => clip.clear(),
        SavedClipboard::Text(text) => clip.set_text(text),
        SavedClipboard::Image(img) => clip.set_image(img),
        SavedClipboard::Native(items) => {
            clip.restore_native(items).or_else(|e| match saved.text() {
                Some(t) => {
                    log::warn!("full clipboard restore failed ({e}); restored text only");
                    clip.set_text(&t)
                }
                None => Err(e),
            })
        }
        SavedClipboard::Unrestorable => {
            if clip.get_text().is_some_and(|t| t.contains(SENTINEL_MARK)) {
                clip.clear()
            } else {
                log::warn!(
                    "original clipboard content could not be saved; leaving the draft on it"
                );
                Ok(())
            }
        }
    }
}

/// Step 4 of the contract: replace the (still selected) text with the draft,
/// then restore the original clipboard.
pub fn inject<C: ClipboardIo, K: KeySim>(
    clip: &mut C,
    keys: &mut K,
    draft: &str,
    mode: &InjectMode,
    saved: &SavedClipboard,
    timing: &Timing,
) -> Result<(), String> {
    let res = match mode {
        InjectMode::Paste => (|| {
            clip.set_text(draft)?;
            timing.pause();
            keys.paste()?;
            if !timing.restore_delay.is_zero() {
                thread::sleep(timing.restore_delay);
            }
            Ok(())
        })(),
        InjectMode::Type => keys.type_text(draft),
    };
    let restored = restore_clipboard(clip, saved);
    res.and(restored)
}

/// Modifier bits from `CGEventFlags` (Shift, Control, Option, Command).
pub const MODIFIER_MASK: u64 = 0x0002_0000 | 0x0004_0000 | 0x0008_0000 | 0x0010_0000;

pub fn modifiers_held(flags: u64) -> bool {
    flags & MODIFIER_MASK != 0
}

// ---------------------------------------------------------------------------
// Real implementations
// ---------------------------------------------------------------------------

pub struct SystemClipboard(arboard::Clipboard);

impl SystemClipboard {
    pub fn new() -> Result<Self, String> {
        arboard::Clipboard::new()
            .map(SystemClipboard)
            .map_err(|e| format!("clipboard unavailable: {e}"))
    }
}

impl ClipboardIo for SystemClipboard {
    fn get_text(&mut self) -> Option<String> {
        self.0.get_text().ok()
    }
    fn set_text(&mut self, text: &str) -> Result<(), String> {
        self.0.set_text(text).map_err(|e| e.to_string())
    }
    fn clear(&mut self) -> Result<(), String> {
        self.0.clear().map_err(|e| e.to_string())
    }
    fn get_image(&mut self) -> Option<ImageSnapshot> {
        let img = self.0.get_image().ok()?;
        Some(ImageSnapshot {
            width: img.width,
            height: img.height,
            rgba: img.bytes.into_owned(),
        })
    }
    fn set_image(&mut self, img: &ImageSnapshot) -> Result<(), String> {
        self.0
            .set_image(arboard::ImageData {
                width: img.width,
                height: img.height,
                bytes: std::borrow::Cow::Borrowed(&img.rgba),
            })
            .map_err(|e| e.to_string())
    }
    #[cfg(target_os = "macos")]
    fn save_native(&mut self) -> Option<Vec<NativeItem>> {
        macos_pasteboard::save(MAX_SNAPSHOT_BYTES)
    }
    #[cfg(target_os = "macos")]
    fn restore_native(&mut self, items: &[NativeItem]) -> Result<(), String> {
        macos_pasteboard::restore(items)
    }
    #[cfg(target_os = "macos")]
    fn has_content(&mut self) -> bool {
        macos_pasteboard::has_content()
    }
}

/// Raw NSPasteboard snapshot / restore (all items, all representations).
#[cfg(target_os = "macos")]
mod macos_pasteboard {
    use super::NativeItem;
    use objc2::rc::{Retained, autoreleasepool};
    use objc2::runtime::ProtocolObject;
    use objc2_app_kit::{NSPasteboard, NSPasteboardItem, NSPasteboardWriting};
    use objc2_foundation::{NSArray, NSData, NSString};

    pub fn save(max_bytes: usize) -> Option<Vec<NativeItem>> {
        autoreleasepool(|_| {
            let pb = NSPasteboard::generalPasteboard();
            let items = pb.pasteboardItems()?;
            let mut out = Vec::with_capacity(items.count());
            let mut total = 0usize;
            for item in items.iter() {
                let mut reps: NativeItem = Vec::new();
                for ty in item.types().iter() {
                    if let Some(data) = item.dataForType(&ty) {
                        let bytes = data.to_vec();
                        total += bytes.len();
                        if total > max_bytes {
                            log::warn!("clipboard snapshot over {max_bytes} bytes; falling back");
                            return None;
                        }
                        reps.push((ty.to_string(), bytes));
                    }
                }
                if !reps.is_empty() {
                    out.push(reps);
                }
            }
            Some(out)
        })
    }

    pub fn restore(items: &[NativeItem]) -> Result<(), String> {
        autoreleasepool(|_| {
            let pb = NSPasteboard::generalPasteboard();
            let objs: Vec<Retained<ProtocolObject<dyn NSPasteboardWriting>>> = items
                .iter()
                .map(|reps| {
                    let item = NSPasteboardItem::new();
                    for (ty, bytes) in reps {
                        item.setData_forType(&NSData::with_bytes(bytes), &NSString::from_str(ty));
                    }
                    ProtocolObject::from_retained(item)
                })
                .collect();
            pb.clearContents();
            if pb.writeObjects(&NSArray::from_retained_slice(&objs)) {
                Ok(())
            } else {
                Err("NSPasteboard rejected the restored items".into())
            }
        })
    }

    pub fn has_content() -> bool {
        autoreleasepool(|_| {
            NSPasteboard::generalPasteboard()
                .types()
                .is_some_and(|t| t.count() > 0)
        })
    }
}

pub struct SystemKeys(enigo::Enigo);

impl SystemKeys {
    pub fn new() -> Result<Self, String> {
        enigo::Enigo::new(&enigo::Settings::default())
            .map(SystemKeys)
            .map_err(|e| format!("keyboard simulation unavailable: {e}"))
    }

    /// Modifier + letter chord. On macOS we use raw virtual keycodes, which
    /// avoids the keyboard-layout lookup (that API must run on the main thread).
    fn chord(&mut self, letter: char) -> Result<(), String> {
        use enigo::{Direction, Key, Keyboard};
        let err = |x: enigo::InputError| x.to_string();
        let e = &mut self.0;

        #[cfg(target_os = "macos")]
        let modifier = Key::Meta;
        #[cfg(not(target_os = "macos"))]
        let modifier = Key::Control;

        // kVK_ANSI_A = 0x00, kVK_ANSI_C = 0x08, kVK_ANSI_V = 0x09
        #[cfg(target_os = "macos")]
        let code: u16 = match letter {
            'a' => 0x00,
            'c' => 0x08,
            'v' => 0x09,
            _ => return Err(format!("unsupported chord letter {letter}")),
        };

        e.key(modifier, Direction::Press).map_err(err)?;
        thread::sleep(Duration::from_millis(20));
        #[cfg(target_os = "macos")]
        let r = e.raw(code, Direction::Click).map_err(err);
        #[cfg(not(target_os = "macos"))]
        let r = e.key(Key::Unicode(letter), Direction::Click).map_err(err);
        thread::sleep(Duration::from_millis(20));
        let released = e.key(modifier, Direction::Release).map_err(err);
        r.and(released)
    }
}

impl KeySim for SystemKeys {
    fn copy(&mut self) -> Result<(), String> {
        self.chord('c')
    }
    fn select_all(&mut self) -> Result<(), String> {
        self.chord('a')
    }
    fn paste(&mut self) -> Result<(), String> {
        self.chord('v')
    }
    fn type_text(&mut self, text: &str) -> Result<(), String> {
        use enigo::Keyboard;
        self.0.text(text).map_err(|e| e.to_string())
    }
}

#[cfg(target_os = "macos")]
fn current_modifier_flags() -> u64 {
    #[link(name = "CoreGraphics", kind = "framework")]
    unsafe extern "C" {
        fn CGEventSourceFlagsState(state_id: i32) -> u64;
    }
    // kCGEventSourceStateHIDSystemState = 1 (physical keyboard state).
    // SAFETY: plain C function taking an enum value.
    unsafe { CGEventSourceFlagsState(1) }
}

/// Block until the hotkey (and every modifier) is physically released, so our
/// simulated Cmd+C isn't read as Cmd+Shift+C.
fn wait_for_trigger_release(state: &AppState) {
    let deadline = Instant::now() + Duration::from_millis(1500);
    while state.trigger_held.load(Ordering::SeqCst) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    #[cfg(target_os = "macos")]
    while modifiers_held(current_modifier_flags()) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    thread::sleep(Duration::from_millis(80));
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RunResult {
    pub ok: bool,
    pub latency_ms: u64,
    pub error: Option<String>,
}

impl RunResult {
    fn err(started: Instant, msg: impl Into<String>) -> Self {
        RunResult {
            ok: false,
            latency_ms: started.elapsed().as_millis() as u64,
            error: Some(msg.into()),
        }
    }
}

/// Run the full loop on the current (non-main) thread. Emits events, updates
/// state and shows notifications on failure.
pub fn run_loop(app: &AppHandle) -> RunResult {
    let state = app.state::<AppState>();
    if state.running.swap(true, Ordering::SeqCst) {
        return RunResult {
            ok: false,
            latency_ms: 0,
            error: Some("Polytone is already running".into()),
        };
    }
    let started = Instant::now();
    let _ = app.emit(
        EVENT_STARTED,
        RunResult {
            ok: true,
            latency_ms: 0,
            error: None,
        },
    );

    let result = run_loop_inner(&state, started);

    if let Some(err) = &result.error {
        log::warn!("hotkey loop failed: {err}");
        notify(app, err);
    } else {
        log::info!("hotkey loop ok in {}ms", result.latency_ms);
    }
    *state.last_run.lock().unwrap() = Some(result.clone());
    state.running.store(false, Ordering::SeqCst);
    let _ = app.emit(EVENT_FINISHED, result.clone());
    result
}

fn run_loop_inner(state: &AppState, started: Instant) -> RunResult {
    if !crate::permissions::accessibility_trusted() {
        let _ = crate::permissions::open_permission_settings("accessibility".into());
        return RunResult::err(
            started,
            "Polytone needs Accessibility permission (System Settings > Privacy & Security)",
        );
    }

    wait_for_trigger_release(state);

    let client = state.client.clone();
    let cached_mode = state.inject_mode.lock().unwrap().clone();
    let mode = match tauri::async_runtime::block_on(client.get_config()) {
        Ok(cfg) => {
            *state.inject_mode.lock().unwrap() = cfg.inject_mode.clone();
            cfg.inject_mode
        }
        Err(e) => {
            log::debug!("get_config failed, using cached inject_mode: {e}");
            cached_mode
        }
    };

    let mut clip = match SystemClipboard::new() {
        Ok(c) => c,
        Err(e) => return RunResult::err(started, e),
    };
    let mut keys = match SystemKeys::new() {
        Ok(k) => k,
        Err(e) => return RunResult::err(started, e),
    };
    let timing = Timing::MACOS;
    let sentinel = make_sentinel();

    let cap = match capture(&mut clip, &mut keys, &sentinel, &timing) {
        Ok(c) => c,
        Err(e) => return RunResult::err(started, e),
    };
    let (intent, source) = match cap.outcome {
        CaptureOutcome::Captured { text, source } => (text, source),
        CaptureOutcome::Empty => {
            let _ = restore_clipboard(&mut clip, &cap.saved);
            return RunResult::err(started, backend_client::MSG_EMPTY);
        }
    };
    log::info!("captured {} chars from {:?}", intent.len(), source);

    let req = GenerateRequest {
        intent,
        persona: None,
        app_context: frontmost_app_name(),
    };
    let draft = match tauri::async_runtime::block_on(client.generate(&req)) {
        Ok(r) => r.text,
        Err(e) => {
            let _ = restore_clipboard(&mut clip, &cap.saved);
            return RunResult::err(started, e.user_message());
        }
    };

    if let Err(e) = inject(&mut clip, &mut keys, &draft, &mode, &cap.saved, &timing) {
        return RunResult::err(started, format!("Polytone could not insert the draft: {e}"));
    }
    // A message sent from this app soon after is (an edit of) the draft: never learn style from it.
    crate::typing_capture::record_injection();

    RunResult {
        ok: true,
        latency_ms: started.elapsed().as_millis() as u64,
        error: None,
    }
}

/// Name of the frontmost app, sent as `app_context` (best-effort, macOS only).
fn frontmost_app_name() -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        let out = std::process::Command::new("osascript")
            .args([
                "-e",
                "tell application \"System Events\" to get name of first application process whose frontmost is true",
            ])
            .output()
            .ok()?;
        if !out.status.success() {
            return None;
        }
        let name = String::from_utf8_lossy(&out.stdout).trim().to_string();
        (!name.is_empty()).then_some(name)
    }
    #[cfg(not(target_os = "macos"))]
    {
        None
    }
}

// ---------------------------------------------------------------------------
// Hotkey registration
// ---------------------------------------------------------------------------

/// Handler passed to the global-shortcut plugin builder.
pub fn on_shortcut(app: &AppHandle, _shortcut: &Shortcut, event: ShortcutEvent) {
    let state = app.state::<AppState>();
    match event.state {
        ShortcutState::Pressed => {
            state.trigger_held.store(true, Ordering::SeqCst);
            if state.paused.load(Ordering::SeqCst) {
                return;
            }
            if state.running.load(Ordering::SeqCst) {
                log::info!("hotkey ignored: loop already running");
                return;
            }
            let app = app.clone();
            thread::spawn(move || {
                run_loop(&app);
            });
        }
        ShortcutState::Released => {
            state.trigger_held.store(false, Ordering::SeqCst);
        }
    }
}

pub fn parse_accelerator(accel: &str) -> Result<Shortcut, String> {
    accel
        .trim()
        .parse::<Shortcut>()
        .map_err(|e| format!("invalid hotkey '{accel}': {e}"))
}

/// Replace the registered hotkey. On failure the previous one stays active.
pub fn register_hotkey(app: &AppHandle, accel: &str) -> Result<(), String> {
    let new = parse_accelerator(accel)?;
    let state = app.state::<AppState>();
    let gs = app.global_shortcut();
    let old_accel = state.hotkey.lock().unwrap().clone();
    let old = parse_accelerator(&old_accel).ok();

    if let Some(old) = old {
        if old == new && gs.is_registered(new) {
            return Ok(());
        }
        if gs.is_registered(old) {
            let _ = gs.unregister(old);
        }
    }
    match gs.register(new) {
        Ok(()) => {
            *state.hotkey.lock().unwrap() = accel.trim().to_string();
            log::info!("hotkey registered: {accel}");
            Ok(())
        }
        Err(e) => {
            if let Some(old) = old {
                let _ = gs.register(old);
            }
            Err(format!("could not register hotkey '{accel}': {e}"))
        }
    }
}

/// Register whatever hotkey is currently in state (used at startup).
pub fn register_initial(app: &AppHandle) {
    let state = app.state::<AppState>();
    let accel = state.hotkey.lock().unwrap().clone();
    match parse_accelerator(&accel) {
        Ok(sc) => {
            if let Err(e) = app.global_shortcut().register(sc) {
                log::error!("could not register hotkey {accel}: {e}");
                notify(app, &format!("Polytone could not register hotkey {accel}"));
            }
        }
        Err(e) => log::error!("{e}"),
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct HotkeyStatus {
    pub hotkey: String,
    pub paused: bool,
    pub last_run: Option<RunResult>,
}

pub fn hotkey_status(state: &AppState) -> HotkeyStatus {
    HotkeyStatus {
        hotkey: state.hotkey.lock().unwrap().clone(),
        paused: state.paused.load(Ordering::SeqCst),
        last_run: state.last_run.lock().unwrap().clone(),
    }
}

#[tauri::command]
pub async fn set_hotkey(app: AppHandle, accel: String) -> Result<HotkeyStatus, String> {
    register_hotkey(&app, &accel)?;
    let state = app.state::<AppState>();
    let saved = state.hotkey.lock().unwrap().clone();
    // Persist to the backend config (best-effort).
    if let Err(e) = state
        .client
        .update_config(|c| c.hotkey = saved.clone())
        .await
    {
        log::warn!("could not persist hotkey to backend: {e}");
    }
    Ok(hotkey_status(&state))
}

#[tauri::command]
pub fn get_hotkey_status(state: tauri::State<'_, AppState>) -> HotkeyStatus {
    hotkey_status(&state)
}

#[tauri::command]
pub fn set_hotkey_paused(app: AppHandle, paused: bool) -> HotkeyStatus {
    crate::tray::set_paused(&app, paused);
    hotkey_status(&app.state::<AppState>())
}

#[tauri::command]
pub async fn trigger_now(app: AppHandle) -> Result<RunResult, String> {
    // Invoked from Polytone's own window, which has focus: hide the app so macOS
    // re-activates the app the user was in, then run the loop against that.
    #[cfg(target_os = "macos")]
    {
        let _ = app.hide();
    }
    tauri::async_runtime::spawn_blocking(move || {
        thread::sleep(Duration::from_millis(350));
        run_loop(&app)
    })
    .await
    .map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    /// Simulated focused text field + clipboard shared between the fakes.
    #[derive(Default)]
    struct World {
        clipboard: Option<String>,
        field: String,
        selection: Option<String>, // currently selected text
        log: Vec<String>,
        fail_copy: bool,
        /// If false, copy does nothing (app ignores Cmd+C, e.g. password field).
        copy_works: bool,
        /// Non-text clipboard content.
        image: Option<ImageSnapshot>,
        /// Opaque content we can't read at all (e.g. a file promise).
        opaque: bool,
        /// Simulate the macOS full-pasteboard snapshot.
        native: bool,
        native_restore_fails: bool,
    }

    type W = Rc<RefCell<World>>;

    struct FakeClip(W);
    struct FakeKeys(W);

    impl ClipboardIo for FakeClip {
        fn get_text(&mut self) -> Option<String> {
            self.0.borrow().clipboard.clone().filter(|s| !s.is_empty())
        }
        fn set_text(&mut self, t: &str) -> Result<(), String> {
            let mut w = self.0.borrow_mut();
            w.log.push(format!("set:{t}"));
            w.clipboard = Some(t.to_string());
            w.image = None;
            w.opaque = false;
            Ok(())
        }
        fn clear(&mut self) -> Result<(), String> {
            let mut w = self.0.borrow_mut();
            w.log.push("clear".into());
            w.clipboard = None;
            w.image = None;
            w.opaque = false;
            Ok(())
        }
        fn get_image(&mut self) -> Option<ImageSnapshot> {
            let w = self.0.borrow();
            w.image.clone().filter(|_| w.clipboard.is_none())
        }
        fn set_image(&mut self, img: &ImageSnapshot) -> Result<(), String> {
            let mut w = self.0.borrow_mut();
            w.log.push("set_image".into());
            w.clipboard = None;
            w.image = Some(img.clone());
            Ok(())
        }
        fn save_native(&mut self) -> Option<Vec<NativeItem>> {
            let w = self.0.borrow();
            if !w.native {
                return None;
            }
            let mut reps: NativeItem = Vec::new();
            if let Some(t) = &w.clipboard {
                reps.push((PLAIN_TEXT_UTI.into(), t.clone().into_bytes()));
                reps.push(("public.rtf".into(), format!("{{\\rtf1 {t}}}").into_bytes()));
            }
            if let Some(img) = &w.image {
                reps.push(("public.png".into(), img.rgba.clone()));
            }
            Some(if reps.is_empty() { vec![] } else { vec![reps] })
        }
        fn restore_native(&mut self, items: &[NativeItem]) -> Result<(), String> {
            let mut w = self.0.borrow_mut();
            if w.native_restore_fails {
                return Err("nope".into());
            }
            w.log.push(format!(
                "restore_native:{}",
                items.iter().map(|i| i.len()).sum::<usize>()
            ));
            w.clipboard = None;
            w.image = None;
            for (ty, data) in items.iter().flatten() {
                match ty.as_str() {
                    PLAIN_TEXT_UTI => w.clipboard = Some(String::from_utf8(data.clone()).unwrap()),
                    "public.png" => {
                        w.image = Some(ImageSnapshot {
                            width: 1,
                            height: 1,
                            rgba: data.clone(),
                        })
                    }
                    _ => {}
                }
            }
            Ok(())
        }
        fn has_content(&mut self) -> bool {
            let w = self.0.borrow();
            w.clipboard.is_some() || w.image.is_some() || w.opaque
        }
    }

    impl KeySim for FakeKeys {
        fn copy(&mut self) -> Result<(), String> {
            let mut w = self.0.borrow_mut();
            w.log.push("copy".into());
            if w.fail_copy {
                return Err("boom".into());
            }
            if !w.copy_works {
                return Ok(());
            }
            // Like real apps: copying an empty selection leaves clipboard untouched.
            if let Some(sel) = w.selection.clone().filter(|s| !s.is_empty()) {
                w.clipboard = Some(sel);
            }
            Ok(())
        }
        fn select_all(&mut self) -> Result<(), String> {
            let mut w = self.0.borrow_mut();
            w.log.push("select_all".into());
            w.selection = Some(w.field.clone());
            Ok(())
        }
        fn paste(&mut self) -> Result<(), String> {
            let mut w = self.0.borrow_mut();
            w.log.push("paste".into());
            let clip = w.clipboard.clone().unwrap_or_default();
            replace_selection(&mut w, &clip);
            Ok(())
        }
        fn type_text(&mut self, t: &str) -> Result<(), String> {
            let mut w = self.0.borrow_mut();
            w.log.push(format!("type:{t}"));
            replace_selection(&mut w, t);
            Ok(())
        }
    }

    fn replace_selection(w: &mut World, text: &str) {
        match w.selection.take() {
            Some(sel) if !sel.is_empty() => w.field = w.field.replacen(&sel, text, 1),
            _ => w.field.push_str(text),
        }
    }

    fn world(clipboard: Option<&str>, field: &str, selection: Option<&str>) -> W {
        Rc::new(RefCell::new(World {
            clipboard: clipboard.map(String::from),
            field: field.into(),
            selection: selection.map(String::from),
            copy_works: true,
            ..Default::default()
        }))
    }

    const S: &str = "<<polytone-sentinel>>";

    fn run_capture(w: &W) -> Result<CaptureResult, String> {
        capture(
            &mut FakeClip(w.clone()),
            &mut FakeKeys(w.clone()),
            S,
            &Timing::INSTANT,
        )
    }

    #[test]
    fn usable_rejects_sentinel_and_blank() {
        assert_eq!(usable(S, None), None);
        assert_eq!(usable(S, Some(S)), None);
        assert_eq!(usable(S, Some("   \n")), None);
        assert_eq!(usable(S, Some("  hi  ")), Some("hi".into()));
    }

    #[test]
    fn sentinel_is_unique_and_not_blank() {
        let a = make_sentinel();
        assert!(!a.trim().is_empty());
        assert!(a.contains("polytone-sentinel"));
    }

    #[test]
    fn captures_selection_first() {
        let w = world(Some("orig"), "hello there friend", Some("there"));
        let r = run_capture(&w).unwrap();
        assert_eq!(
            r.outcome,
            CaptureOutcome::Captured {
                text: "there".into(),
                source: CaptureSource::Selection
            }
        );
        assert_eq!(r.original.as_deref(), Some("orig"));
        assert!(!w.borrow().log.contains(&"select_all".to_string()));
    }

    #[test]
    fn falls_back_to_whole_field() {
        let w = world(Some("orig"), "rough intent text", None);
        let r = run_capture(&w).unwrap();
        assert_eq!(
            r.outcome,
            CaptureOutcome::Captured {
                text: "rough intent text".into(),
                source: CaptureSource::Field
            }
        );
        let log = w.borrow().log.clone();
        assert_eq!(
            log,
            vec![
                format!("set:{S}"),
                "copy".into(),
                "select_all".into(),
                "copy".into()
            ]
        );
    }

    #[test]
    fn falls_back_to_original_clipboard_when_field_empty() {
        let w = world(Some("from clipboard"), "", None);
        let r = run_capture(&w).unwrap();
        assert_eq!(
            r.outcome,
            CaptureOutcome::Captured {
                text: "from clipboard".into(),
                source: CaptureSource::Clipboard
            }
        );
    }

    #[test]
    fn falls_back_to_clipboard_when_app_ignores_copy() {
        let w = world(Some("from clipboard"), "secret", None);
        w.borrow_mut().copy_works = false;
        let r = run_capture(&w).unwrap();
        assert_eq!(
            r.outcome,
            CaptureOutcome::Captured {
                text: "from clipboard".into(),
                source: CaptureSource::Clipboard
            }
        );
    }

    #[test]
    fn empty_everywhere_is_empty() {
        let w = world(None, "", None);
        let r = run_capture(&w).unwrap();
        assert_eq!(r.outcome, CaptureOutcome::Empty);
        assert_eq!(r.original, None);

        let w = world(Some("   "), "  ", None);
        let r = run_capture(&w).unwrap();
        assert_eq!(r.outcome, CaptureOutcome::Empty);
    }

    #[test]
    fn whitespace_selection_falls_through_to_field() {
        let w = world(None, "  real text ", Some("   "));
        let r = run_capture(&w).unwrap();
        assert_eq!(
            r.outcome,
            CaptureOutcome::Captured {
                text: "real text".into(),
                source: CaptureSource::Field
            }
        );
    }

    #[test]
    fn capture_error_restores_original_clipboard() {
        let w = world(Some("orig"), "x", None);
        w.borrow_mut().fail_copy = true;
        assert!(run_capture(&w).is_err());
        assert_eq!(w.borrow().clipboard.as_deref(), Some("orig"));
    }

    #[test]
    fn paste_inject_replaces_field_and_restores_clipboard() {
        let w = world(Some("orig"), "rough intent", None);
        let r = run_capture(&w).unwrap();
        inject(
            &mut FakeClip(w.clone()),
            &mut FakeKeys(w.clone()),
            "Polished draft.",
            &InjectMode::Paste,
            &r.saved,
            &Timing::INSTANT,
        )
        .unwrap();
        let w = w.borrow();
        assert_eq!(w.field, "Polished draft.");
        assert_eq!(w.clipboard.as_deref(), Some("orig"));
    }

    #[test]
    fn paste_inject_replaces_only_selection() {
        let w = world(None, "Hi, rough bit. Bye", Some("rough bit"));
        let r = run_capture(&w).unwrap();
        inject(
            &mut FakeClip(w.clone()),
            &mut FakeKeys(w.clone()),
            "nice bit",
            &InjectMode::Paste,
            &r.saved,
            &Timing::INSTANT,
        )
        .unwrap();
        let w = w.borrow();
        assert_eq!(w.field, "Hi, nice bit. Bye");
        // original clipboard was empty -> cleared afterwards
        assert_eq!(w.clipboard, None);
        assert_eq!(w.log.last().map(String::as_str), Some("clear"));
    }

    #[test]
    fn type_inject_types_and_restores_clipboard() {
        let w = world(Some("orig"), "rough", None);
        let r = run_capture(&w).unwrap();
        inject(
            &mut FakeClip(w.clone()),
            &mut FakeKeys(w.clone()),
            "Typed draft",
            &InjectMode::Type,
            &r.saved,
            &Timing::INSTANT,
        )
        .unwrap();
        let w = w.borrow();
        assert_eq!(w.field, "Typed draft");
        assert_eq!(w.clipboard.as_deref(), Some("orig"));
        assert!(!w.log.contains(&"paste".to_string()));
    }

    fn paste_run(w: &W, draft: &str) -> CaptureResult {
        let r = run_capture(w).unwrap();
        inject(
            &mut FakeClip(w.clone()),
            &mut FakeKeys(w.clone()),
            draft,
            &InjectMode::Paste,
            &r.saved,
            &Timing::INSTANT,
        )
        .unwrap();
        r
    }

    fn img() -> ImageSnapshot {
        ImageSnapshot {
            width: 1,
            height: 1,
            rgba: vec![1, 2, 3, 255],
        }
    }

    #[test]
    fn image_clipboard_is_restored_not_cleared() {
        let w = world(None, "rough intent", None);
        w.borrow_mut().image = Some(img());
        let r = paste_run(&w, "Draft.");
        assert_eq!(r.saved, SavedClipboard::Image(img()));
        let w = w.borrow();
        assert_eq!(w.field, "Draft.");
        assert_eq!(w.image, Some(img()));
        assert_eq!(w.clipboard, None);
        assert!(!w.log.contains(&"clear".to_string()));
    }

    #[test]
    fn native_snapshot_restores_every_representation() {
        let w = world(Some("orig"), "rough intent", None);
        w.borrow_mut().native = true;
        let r = paste_run(&w, "Draft.");
        assert!(matches!(&r.saved, SavedClipboard::Native(items) if items[0].len() == 2));
        assert_eq!(r.original.as_deref(), Some("orig"));
        let w = w.borrow();
        assert_eq!(w.field, "Draft.");
        assert_eq!(w.clipboard.as_deref(), Some("orig"));
        assert_eq!(w.log.last().map(String::as_str), Some("restore_native:2"));
    }

    #[test]
    fn native_text_still_works_as_capture_fallback() {
        let w = world(Some("from clipboard"), "", None);
        w.borrow_mut().native = true;
        let r = run_capture(&w).unwrap();
        assert_eq!(
            r.outcome,
            CaptureOutcome::Captured {
                text: "from clipboard".into(),
                source: CaptureSource::Clipboard
            }
        );
    }

    #[test]
    fn native_restore_failure_falls_back_to_text() {
        let w = world(Some("orig"), "rough", None);
        {
            let mut b = w.borrow_mut();
            b.native = true;
            b.native_restore_fails = true;
        }
        paste_run(&w, "Draft.");
        assert_eq!(w.borrow().clipboard.as_deref(), Some("orig"));
    }

    #[test]
    fn unreadable_clipboard_is_not_clobbered_with_clear() {
        let w = world(None, "rough intent", None);
        w.borrow_mut().opaque = true;
        let r = paste_run(&w, "Draft.");
        assert_eq!(r.saved, SavedClipboard::Unrestorable);
        let w = w.borrow();
        assert_eq!(w.field, "Draft.");
        // We could not save it, so we leave the draft rather than wiping the clipboard.
        assert_eq!(w.clipboard.as_deref(), Some("Draft."));
        assert!(!w.log.contains(&"clear".to_string()));
    }

    #[test]
    fn unreadable_clipboard_sentinel_is_removed_on_empty_capture() {
        let w = world(None, "", None);
        w.borrow_mut().opaque = true;
        let r = run_capture(&w).unwrap();
        assert_eq!(r.outcome, CaptureOutcome::Empty);
        restore_clipboard(&mut FakeClip(w.clone()), &r.saved).unwrap();
        assert_eq!(w.borrow().clipboard, None);
    }

    /// Touches the real macOS pasteboard (and puts the user's content back). Run with
    /// `cargo test -- --ignored real_pasteboard`.
    #[cfg(target_os = "macos")]
    #[test]
    #[ignore]
    fn real_pasteboard_image_round_trip() {
        let mut clip = SystemClipboard::new().unwrap();
        let users = save_clipboard(&mut clip);
        let pixel = ImageSnapshot {
            width: 2,
            height: 1,
            rgba: vec![255, 0, 0, 255, 0, 0, 255, 255],
        };
        clip.set_image(&pixel).unwrap();
        let saved = save_clipboard(&mut clip);
        assert!(matches!(saved, SavedClipboard::Native(_)), "{saved:?}");
        clip.set_text("draft text").unwrap();
        restore_clipboard(&mut clip, &saved).unwrap();
        let back = clip.get_image();
        let text = clip.get_text();
        restore_clipboard(&mut clip, &users).unwrap();
        assert_eq!(back.map(|i| (i.width, i.height)), Some((2, 1)));
        assert_eq!(text, None);
    }

    #[test]
    fn modifier_mask() {
        assert!(!modifiers_held(0));
        assert!(!modifiers_held(0x100)); // non-coalesced/device bits only
        assert!(modifiers_held(0x0002_0000)); // shift
        assert!(modifiers_held(0x0010_0000)); // command
        assert!(modifiers_held(0x0012_0000));
    }

    #[test]
    fn parses_accelerators() {
        assert!(parse_accelerator(DEFAULT_HOTKEY).is_ok());
        assert!(parse_accelerator("Alt+Space").is_ok());
        assert!(parse_accelerator("NotAKey+++").is_err());
        assert!(parse_accelerator("").is_err());
    }
}
