//! Pure typing-capture policy: which keys send, which fields count, text filters, dedupe and
//! the after-inject window. No OS calls, unit-tested.

use std::collections::HashMap;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub use crate::macos::keycodes::{
    FLAG_ALT, FLAG_COMMAND, FLAG_CONTROL, FLAG_SHIFT, KC_D, KC_KEYPAD_ENTER, KC_RETURN,
};

/// After an edit key, read the field once the app has applied the key (the tap sees a key
/// before the app does). Keys inside this delay share the read, so reads stay ≤ ~25/s.
pub const READ_DELAY: Duration = Duration::from_millis(40);
/// ...and once more after typing pauses (apps that update their AX value asynchronously).
pub const DEBOUNCE: Duration = Duration::from_millis(250);
pub const MIN_WORDS: usize = 3;
pub const MAX_CHARS: usize = 5000;
/// A send this soon after a Polytone inject in the same app is (an edit of) a draft.
pub const INJECT_WINDOW: Duration = Duration::from_secs(600);
/// After a send key: re-read the field at these offsets; a send clears it.
pub const CONFIRM_DELAYS: [Duration; 2] = [Duration::from_millis(250), Duration::from_millis(900)];

pub const MAIL_BUNDLE: &str = "com.apple.mail";
/// Apps where a bare Return inserts a newline rather than sending.
pub const RETURN_IS_NEWLINE: &[&str] = &["com.apple.mail", "com.apple.Notes", "notion.id"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyAction {
    /// The user may be sending the message: capture it.
    Send,
    /// Anything else: the field text may have changed, re-read it.
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

/// Browsers whose single-line fields are mostly the address bar and search boxes.
pub const BROWSERS: &[&str] = &[
    "com.google.Chrome",
    "com.apple.Safari",
    "company.thebrowser.Browser",
    "org.mozilla.firefox",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "com.vivaldi.Vivaldi",
    "org.chromium.Chromium",
    "com.operasoftware.Opera",
];

/// Chromium browsers: their web content has no AX tree until an assistive client sets
/// `AXEnhancedUserInterface` on the app (they don't support Electron's `AXManualAccessibility`).
pub const CHROMIUM_BROWSERS: &[&str] = &[
    "com.google.Chrome",
    "company.thebrowser.Browser",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "com.vivaldi.Vivaldi",
    "org.chromium.Chromium",
    "com.operasoftware.Opera",
];

pub fn needs_enhanced_ui(bundle_id: &str) -> bool {
    CHROMIUM_BROWSERS.contains(&bundle_id)
}

/// Which focused element roles hold text worth learning from.
///
/// In browsers, web-content apps (Electron: Slack, Discord, ... — `web_content`) and Mail only
/// multi-line message boxes (textarea / contenteditable → AXTextArea) count: Return in an
/// address bar, a search box, a "jump to" box or Mail's To/Subject field would otherwise look
/// like a sent message. Native chat apps also use single-line fields (Messages' composer is an
/// AXTextField).
pub fn text_role_allowed(role: &str, bundle_id: &str, web_content: bool) -> bool {
    if web_content || BROWSERS.contains(&bundle_id) || bundle_id == MAIL_BUNDLE {
        return role == "AXTextArea";
    }
    matches!(role, "AXTextArea" | "AXTextField" | "AXComboBox")
}

/// Why a key did not lead to a capture (debug log + `CaptureStatus::last_skip`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Skip {
    Off,
    Paused,
    SecureInput,
    NoFrontApp,
    NotAllowlisted(String),
    /// This process isn't trusted for Accessibility, so no field can be read.
    NoAccessibility,
    /// Nothing focused, or the focused element can't be read (no Accessibility permission, or
    /// a web-content app that hasn't built its AX tree yet).
    NoFocusedElement,
    OtherProcess,
    PasswordField,
    Role(String),
    NoValue,
    TooShort,
    SlashCommand,
    Duplicate,
    NotConfirmed,
}

impl std::fmt::Display for Skip {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Skip::Off => f.write_str("capture is off"),
            Skip::Paused => f.write_str("the shortcut is paused"),
            Skip::SecureInput => f.write_str("secure input is on (a password field is active)"),
            Skip::NoFrontApp => f.write_str("no frontmost app"),
            Skip::NotAllowlisted(b) => write!(f, "{b} is not in the app list"),
            Skip::NoAccessibility => f.write_str("Accessibility permission is missing"),
            Skip::NoFocusedElement => f.write_str("no readable focused text field"),
            Skip::OtherProcess => f.write_str("focus is in another process"),
            Skip::PasswordField => f.write_str("password or search field"),
            Skip::Role(r) => write!(f, "the focused element is not a message box ({r})"),
            Skip::NoValue => f.write_str("the field exposes no text (AXValue)"),
            Skip::TooShort => write!(f, "fewer than {MIN_WORDS} words"),
            Skip::SlashCommand => f.write_str("slash command"),
            Skip::Duplicate => f.write_str("same text as the last message sent in this app"),
            Skip::NotConfirmed => f.write_str("the field did not clear (Return was a newline)"),
        }
    }
}

pub fn word_count(text: &str) -> usize {
    text.split_whitespace()
        .filter(|w| w.chars().any(char::is_alphanumeric))
        .count()
}

/// Trim, drop items under [`MIN_WORDS`] words and slash commands (`/remind me ...`), cap at
/// [`MAX_CHARS`] characters (cut back to a word boundary when one is near).
pub fn prepare_text(text: &str) -> Result<String, Skip> {
    let t = text.trim();
    if word_count(t) < MIN_WORDS {
        return Err(Skip::TooShort);
    }
    if t.starts_with('/') && !t.starts_with("//") {
        return Err(Skip::SlashCommand);
    }
    if t.chars().count() <= MAX_CHARS {
        return Ok(t.to_string());
    }
    let end = t.char_indices().nth(MAX_CHARS).map_or(t.len(), |(i, _)| i);
    let head = &t[..end];
    let cut = match head.rfind(char::is_whitespace) {
        Some(i) if i > end / 2 => &head[..i],
        _ => head,
    };
    Ok(cut.trim_end().to_string())
}

/// Text to capture on a send key: the cached read, unless an immediate read of the *same*
/// element still has (non-empty) text — that one includes the last few keystrokes.
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

/// Did the send key really send? `value_now` is a fresh read of the element the text was in
/// (not of whatever has focus now: an app switch right after a newline must not count).
///
/// Sent: the element is gone or unreadable (Mail's compose window closed, Slack's inline
/// editor closed), it is empty, or it holds something unrelated (the next message).
/// Not sent: it still contains the text (Return was a newline), or it still holds most of it
/// (shared prefix + suffix ≥ half the text) because Return accepted an autocomplete
/// (`@jo` → `@John Smith`, `:smi` → `:smile:`) or committed an IME composition.
pub fn confirm_sent(text: &str, value_now: Option<&str>) -> bool {
    let Some(v) = value_now else { return true };
    let (v, t) = (v.trim(), text.trim());
    if v.is_empty() {
        return true;
    }
    if v.contains(t) {
        return false;
    }
    let (vc, tc): (Vec<char>, Vec<char>) = (v.chars().collect(), t.chars().collect());
    let prefix = vc.iter().zip(&tc).take_while(|(a, b)| a == b).count();
    let max_suffix = vc.len().min(tc.len()) - prefix;
    let suffix = vc
        .iter()
        .rev()
        .zip(tc.iter().rev())
        .take(max_suffix)
        .take_while(|(a, b)| a == b)
        .count();
    (prefix + suffix) * 2 < tc.len()
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

#[cfg(test)]
mod tests {
    use super::*;

    const SLACK: &str = "com.tinyspeck.slackmacgap";

    #[test]
    fn browsers_only_read_message_boxes() {
        assert!(text_role_allowed("AXTextArea", "com.google.Chrome", false));
        assert!(!text_role_allowed(
            "AXTextField",
            "com.google.Chrome",
            false
        ));
        assert!(!text_role_allowed("AXComboBox", "com.apple.Safari", false));
        assert!(!text_role_allowed("AXWebArea", "com.google.Chrome", false));
        // Messages' composer is a single-line field
        assert!(text_role_allowed(
            "AXTextField",
            "com.apple.MobileSMS",
            false
        ));
        assert!(!text_role_allowed("AXButton", "com.apple.MobileSMS", false));
    }

    #[test]
    fn web_content_apps_and_mail_only_read_message_boxes() {
        // Electron (Slack/Discord): the composer is a contenteditable AXTextArea; search and
        // "jump to" boxes are AXTextField / AXComboBox and must not count as sends.
        assert!(text_role_allowed("AXTextArea", SLACK, true));
        assert!(!text_role_allowed("AXTextField", SLACK, true));
        assert!(!text_role_allowed("AXComboBox", "com.hnc.Discord", true));
        // Mail: the body (WebKit contenteditable → AXTextArea) counts, To/Subject don't
        assert!(text_role_allowed("AXTextArea", MAIL_BUNDLE, false));
        assert!(!text_role_allowed("AXTextField", MAIL_BUNDLE, false));
    }

    #[test]
    fn chromium_browsers_need_enhanced_ui() {
        assert!(needs_enhanced_ui("com.google.Chrome"));
        assert!(needs_enhanced_ui("company.thebrowser.Browser"));
        assert!(!needs_enhanced_ui("com.apple.Safari"));
        assert!(!needs_enhanced_ui(SLACK)); // Electron: AXManualAccessibility instead
        assert!(CHROMIUM_BROWSERS.iter().all(|b| BROWSERS.contains(b)));
    }

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
        assert_eq!(classify_key(KC_RETURN, 0, "notion.id"), KeyAction::Edit);
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
        assert_eq!(prepare_text("  ok sure  "), Err(Skip::TooShort));
        assert_eq!(prepare_text("ok — sure ?"), Err(Skip::TooShort)); // punctuation isn't a word
        assert_eq!(
            prepare_text(" sounds good to me \n").as_deref(),
            Ok("sounds good to me")
        );
        assert_eq!(
            prepare_text("/remind me to call mom at 5pm"),
            Err(Skip::SlashCommand)
        );
        assert!(prepare_text("// note: this is not a command").is_ok());
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
        assert!(confirm_sent("hi there you", Some("")));
        assert!(confirm_sent("hi there you", Some("  \n")));
        assert!(confirm_sent("hi there you", None)); // element gone (window closed)
        assert!(!confirm_sent("hi there you", Some("hi there you\n"))); // newline
        assert!(!confirm_sent(
            "hi there you",
            Some("hi there you\nand more")
        ));
        assert!(confirm_sent("hi there you", Some("n"))); // already typing the next one
        assert!(confirm_sent("hi there you", Some("Message #general"))); // placeholder
    }

    #[test]
    fn return_accepting_an_autocomplete_is_not_a_send() {
        // mention / emoji completion rewrites the end of the text
        assert!(!confirm_sent("ping @jo", Some("ping @John Smith ")));
        assert!(!confirm_sent(
            "that is so funny :smi",
            Some("that is so funny 😄 ")
        ));
        // ... or the middle (caret before the end)
        assert!(!confirm_sent(
            "hey @jo can you review this",
            Some("hey @Jordan Lee can you review this")
        ));
        // IME commit: kana → kanji at the end of a mostly-Latin message
        assert!(!confirm_sent(
            "see you at the にほんご",
            Some("see you at the 日本語")
        ));
    }

    #[test]
    fn iso_format() {
        assert_eq!(iso_utc(UNIX_EPOCH), "1970-01-01T00:00:00.000Z");
        let t = UNIX_EPOCH + Duration::from_millis(1_793_356_245_123); // 2026-10-30
        assert_eq!(iso_utc(t), "2026-10-30T10:30:45.123Z");
        let leap = UNIX_EPOCH + Duration::from_secs(951_782_400); // 2000-02-29
        assert_eq!(iso_utc(leap), "2000-02-29T00:00:00.000Z");
    }
}
