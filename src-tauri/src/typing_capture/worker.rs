//! The capture worker: gates keys, reads the focused field, confirms sends and dispatches them.
//!
//! Every OS call goes through [`CaptureOs`] (macOS: `os::MacOs`) and time is passed in, so the
//! state machine is unit-tested with a scripted fake (see the tests below).

use std::collections::HashMap;
use std::time::Instant;

use crate::backend_client::TypedIngestRequest;

use super::policy::{
    CONFIRM_DELAYS, DEBOUNCE, KeyAction, READ_DELAY, SentLog, Skip, classify_key, confirm_sent,
    pick_text, prepare_text, text_role_allowed,
};

/// The app keystrokes go to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Front {
    pub bundle_id: String,
    pub name: Option<String>,
    pub pid: i32,
}

/// What an app process exposes, learned once per pid by [`CaptureOs::prepare`].
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AppInfo {
    /// Electron / web-content app (it answers `AXManualAccessibility`): only AXTextArea counts.
    pub web_content: bool,
}

/// The focused UI element, before any policy is applied.
pub struct FocusedRaw<E> {
    pub element: E,
    pub pid: Option<i32>,
    pub role: String,
    pub subrole: String,
}

pub trait CaptureOs {
    type El: PartialEq;
    /// `Ok` while capture is on and the hotkey isn't paused.
    fn active(&self) -> Result<(), Skip>;
    fn secure_input(&self) -> bool;
    fn frontmost(&self) -> Option<Front>;
    fn allowlisted(&self, bundle_id: &str) -> bool;
    /// First key seen in a process: opt it into building its AX tree; report what it is.
    fn prepare(&self, front: &Front) -> AppInfo;
    /// The element with keyboard focus in the app `pid`.
    fn focused(&self, pid: i32) -> Result<FocusedRaw<Self::El>, Skip>;
    /// A fresh `AXValue` read of `el` (None: gone / unreadable / not a string).
    fn value(&self, el: &Self::El) -> Option<String>;
    fn after_inject(&self, bundle_id: &str) -> bool;
    fn dispatch(&self, req: TypedIngestRequest);
    /// A send key that didn't lead to a capture (logging / status).
    fn skipped(&self, bundle_id: &str, why: &Skip);
}

pub struct Focused<E> {
    pub element: E,
    pub value: Option<String>,
}

/// Read the focused text element of `front` (Err for password fields / non-text / other pid).
pub fn read_focused<O: CaptureOs>(
    os: &O,
    front: &Front,
    info: AppInfo,
) -> Result<Focused<O::El>, Skip> {
    let raw = os.focused(front.pid)?;
    if raw.pid != Some(front.pid) {
        return Err(Skip::OtherProcess);
    }
    if raw.subrole == "AXSecureTextField" || raw.subrole == "AXSearchField" {
        return Err(Skip::PasswordField);
    }
    if !text_role_allowed(&raw.role, &front.bundle_id, info.web_content) {
        return Err(Skip::Role(if raw.role.is_empty() {
            "no role".into()
        } else {
            raw.role
        }));
    }
    let value = os.value(&raw.element);
    Ok(Focused {
        element: raw.element,
        value,
    })
}

struct Cached<E> {
    text: String,
    element: E,
}

struct Pending<E> {
    bundle_id: String,
    app_name: Option<String>,
    element: E,
    text: String,
    after_inject: bool,
    started: Instant,
    check: usize,
}

impl<E> Pending<E> {
    fn due(&self) -> Instant {
        self.started + CONFIRM_DELAYS[self.check]
    }
}

pub struct Worker<O: CaptureOs> {
    pub os: O,
    cache: HashMap<String, Cached<O::El>>,
    /// Read the field shortly after an edit key ([`READ_DELAY`]) ...
    read_at: Option<Instant>,
    /// ... and again once typing pauses ([`DEBOUNCE`]).
    settle_at: Option<Instant>,
    pending: Vec<Pending<O::El>>,
    sent: SentLog,
    apps: HashMap<i32, AppInfo>,
}

impl<O: CaptureOs> Worker<O> {
    pub fn new(os: O) -> Self {
        Worker {
            os,
            cache: HashMap::new(),
            read_at: None,
            settle_at: None,
            pending: Vec::new(),
            sent: SentLog::default(),
            apps: HashMap::new(),
        }
    }

    /// When [`Worker::tick`] next has something to do.
    pub fn next_due(&self) -> Option<Instant> {
        self.pending
            .iter()
            .map(Pending::due)
            .chain(self.read_at)
            .chain(self.settle_at)
            .min()
    }

    /// Gate: capture on, not paused, no secure input, frontmost app allowlisted.
    fn gate(&mut self) -> Result<(Front, AppInfo), Skip> {
        self.os.active()?;
        if self.os.secure_input() {
            self.cache.clear();
            return Err(Skip::SecureInput);
        }
        let front = self.os.frontmost().ok_or(Skip::NoFrontApp)?;
        if !self.os.allowlisted(&front.bundle_id) {
            return Err(Skip::NotAllowlisted(front.bundle_id));
        }
        let os = &self.os;
        let info = *self
            .apps
            .entry(front.pid)
            .or_insert_with(|| os.prepare(&front));
        Ok((front, info))
    }

    pub fn on_key(&mut self, keycode: i64, flags: u64, now: Instant) {
        let (front, info) = match self.gate() {
            Ok(g) => g,
            Err(why) => {
                if matches!(
                    keycode,
                    super::policy::KC_RETURN | super::policy::KC_KEYPAD_ENTER
                ) {
                    self.os.skipped("", &why);
                }
                return;
            }
        };
        match classify_key(keycode, flags, &front.bundle_id) {
            KeyAction::Edit => {
                self.read_at.get_or_insert(now + READ_DELAY);
                self.settle_at = Some(now + DEBOUNCE);
            }
            KeyAction::Send => self.on_send(front, info, now),
        }
    }

    fn refresh_cache(&mut self) {
        let Ok((front, info)) = self.gate() else {
            return;
        };
        match read_focused(&self.os, &front, info) {
            Ok(Focused {
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

    fn on_send(&mut self, front: Front, info: AppInfo, now: Instant) {
        self.read_at = None;
        self.settle_at = None;
        let cached = self.cache.remove(&front.bundle_id);
        let immediate = read_focused(&self.os, &front, info);
        let read = immediate.as_ref().ok();
        let same_el = match (&cached, read) {
            (Some(c), Some(n)) => c.element == n.element,
            (None, Some(_)) => true,
            _ => false,
        };
        let text = pick_text(
            cached.as_ref().map(|c| c.text.as_str()),
            read.and_then(|n| n.value.as_deref()),
            same_el,
        );
        let text = match text {
            Some(t) => prepare_text(&t),
            None => Err(match &immediate {
                Err(why) => why.clone(),
                Ok(_) => Skip::NoValue,
            }),
        };
        let text = match text {
            Ok(t) => t,
            Err(why) => {
                self.os.skipped(&front.bundle_id, &why);
                return;
            }
        };
        // the element the text lives in: confirm the send by watching it clear
        let element = match (cached, immediate) {
            (_, Ok(n)) if same_el => n.element,
            (Some(c), _) => c.element,
            (None, Ok(n)) => n.element,
            (None, Err(_)) => return,
        };
        self.pending.push(Pending {
            after_inject: self.os.after_inject(&front.bundle_id),
            bundle_id: front.bundle_id,
            app_name: front.name,
            element,
            text,
            started: now,
            check: 0,
        });
    }

    pub fn tick(&mut self, now: Instant) {
        let read_due = self.read_at.is_some_and(|t| t <= now);
        let settle_due = self.settle_at.is_some_and(|t| t <= now);
        if read_due {
            self.read_at = None;
        }
        if settle_due {
            self.settle_at = None;
        }
        if read_due || settle_due {
            self.refresh_cache();
        }
        let mut i = 0;
        while i < self.pending.len() {
            if self.pending[i].due() > now {
                i += 1;
                continue;
            }
            let mut p = self.pending.swap_remove(i);
            // re-read the element the text was in, wherever focus is now
            let value = self.os.value(&p.element);
            if confirm_sent(&p.text, value.as_deref()) {
                self.send(p);
            } else if p.check + 1 < CONFIRM_DELAYS.len() {
                p.check += 1;
                self.pending.push(p);
            } else {
                self.os.skipped(&p.bundle_id, &Skip::NotConfirmed);
            }
        }
    }

    fn send(&mut self, p: Pending<O::El>) {
        if let Err(why) = self.os.active() {
            self.os.skipped(&p.bundle_id, &why);
            return;
        }
        if !self.sent.should_send(&p.bundle_id, &p.text) {
            self.os.skipped(&p.bundle_id, &Skip::Duplicate);
            return;
        }
        self.os.dispatch(TypedIngestRequest {
            text: p.text,
            app: p.bundle_id,
            app_name: p.app_name,
            after_inject: p.after_inject,
        });
    }
}

#[cfg(test)]
mod tests {
    //! A scripted OS: "apps" own text elements by id; the test types into them (as the app
    //! would apply keys), moves focus and advances a virtual clock.
    use super::*;
    use crate::typing_capture::policy::{FLAG_SHIFT, KC_RETURN};
    use std::cell::RefCell;
    use std::collections::HashMap;
    use std::time::Duration;

    const SLACK: &str = "com.tinyspeck.slackmacgap";
    const MESSAGES: &str = "com.apple.MobileSMS";
    const TERMINAL: &str = "com.apple.Terminal";
    const KEY_A: i64 = 0;

    #[derive(Clone)]
    struct El {
        pid: i32,
        role: &'static str,
        subrole: &'static str,
    }

    #[derive(Default)]
    struct State {
        front: Option<Front>,
        focus: Option<u32>,
        els: HashMap<u32, El>,
        values: HashMap<u32, String>,
        secure: bool,
        off: bool,
        web: Vec<i32>,
        prepared: Vec<i32>,
        injected_in: Option<String>,
        sent: Vec<TypedIngestRequest>,
        skips: Vec<Skip>,
        reads: usize,
    }

    #[derive(Default)]
    struct Fake(RefCell<State>);

    impl CaptureOs for Fake {
        type El = u32;
        fn active(&self) -> Result<(), Skip> {
            if self.0.borrow().off {
                Err(Skip::Off)
            } else {
                Ok(())
            }
        }
        fn secure_input(&self) -> bool {
            self.0.borrow().secure
        }
        fn frontmost(&self) -> Option<Front> {
            self.0.borrow().front.clone()
        }
        fn allowlisted(&self, b: &str) -> bool {
            b != TERMINAL
        }
        fn prepare(&self, f: &Front) -> AppInfo {
            let mut s = self.0.borrow_mut();
            s.prepared.push(f.pid);
            AppInfo {
                web_content: s.web.contains(&f.pid),
            }
        }
        fn focused(&self, _pid: i32) -> Result<FocusedRaw<u32>, Skip> {
            let s = self.0.borrow();
            let id = s.focus.ok_or(Skip::NoFocusedElement)?;
            let el = &s.els[&id];
            Ok(FocusedRaw {
                element: id,
                pid: Some(el.pid),
                role: el.role.into(),
                subrole: el.subrole.into(),
            })
        }
        fn value(&self, el: &u32) -> Option<String> {
            let mut s = self.0.borrow_mut();
            s.reads += 1;
            s.values.get(el).cloned()
        }
        fn after_inject(&self, b: &str) -> bool {
            self.0.borrow().injected_in.as_deref() == Some(b)
        }
        fn dispatch(&self, req: TypedIngestRequest) {
            self.0.borrow_mut().sent.push(req);
        }
        fn skipped(&self, _b: &str, why: &Skip) {
            self.0.borrow_mut().skips.push(why.clone());
        }
    }

    struct Sim {
        w: Worker<Fake>,
        now: Instant,
    }

    impl Sim {
        fn new() -> Sim {
            let mut s = Sim {
                w: Worker::new(Fake::default()),
                now: Instant::now(),
            };
            s.app(SLACK, 100, false);
            s.el(1, 100, "AXTextArea", "");
            s.focus(Some(1));
            s
        }
        fn st(&self) -> std::cell::RefMut<'_, State> {
            self.w.os.0.borrow_mut()
        }
        fn app(&mut self, bundle: &str, pid: i32, web: bool) {
            let mut s = self.st();
            s.front = Some(Front {
                bundle_id: bundle.into(),
                name: Some(bundle.rsplit('.').next().unwrap().into()),
                pid,
            });
            if web {
                s.web.push(pid);
            }
        }
        fn el(&mut self, id: u32, pid: i32, role: &'static str, subrole: &'static str) {
            let mut s = self.st();
            s.els.insert(id, El { pid, role, subrole });
            s.values.insert(id, String::new());
        }
        fn focus(&mut self, id: Option<u32>) {
            self.st().focus = id;
        }
        fn set(&mut self, id: u32, v: &str) {
            self.st().values.insert(id, v.into());
        }
        /// Advance the clock, running every tick that falls due (like the worker loop).
        fn wait(&mut self, ms: u64) {
            let end = self.now + Duration::from_millis(ms);
            while let Some(t) = self.w.next_due().filter(|t| *t <= end) {
                self.now = self.now.max(t);
                self.w.tick(self.now);
            }
            self.now = end;
        }
        /// The user types `text` into the focused element: keys reach the worker first, then
        /// the app applies them (`set`), 120 ms per key.
        fn type_text(&mut self, text: &str) {
            let id = self.st().focus.unwrap();
            let mut cur = self.st().values[&id].clone();
            for ch in text.chars() {
                self.w.on_key(KEY_A, 0, self.now);
                cur.push(ch);
                self.set(id, &cur);
                self.wait(120);
            }
        }
        fn key(&mut self, keycode: i64, flags: u64) {
            self.w.on_key(keycode, flags, self.now);
        }
        fn sent(&self) -> Vec<String> {
            self.st().sent.iter().map(|r| r.text.clone()).collect()
        }
        fn skips(&self) -> Vec<Skip> {
            self.st().skips.clone()
        }
    }

    #[test]
    fn return_that_clears_the_field_is_captured() {
        let mut s = Sim::new();
        s.type_text("see you at noon");
        s.key(KC_RETURN, 0);
        s.wait(80);
        s.set(1, ""); // Slack clears its composer asynchronously
        s.wait(1000);
        assert_eq!(s.sent(), vec!["see you at noon"]);
        let r = s.st().sent[0].clone();
        assert_eq!(r.app, SLACK);
        assert!(!r.after_inject);
        assert_eq!(s.st().prepared, vec![100]); // AX opt-in once per process
    }

    #[test]
    fn last_keystrokes_are_kept_when_the_app_clears_before_the_immediate_read() {
        // A native app processes Return synchronously: by the time the worker reads, the field
        // is already empty. The read shortly after the last key must have the whole text.
        let mut s = Sim::new();
        s.type_text("see you at noo");
        s.key(KEY_A, 0);
        s.set(1, "see you at noon");
        s.wait(60); // Return 60 ms after the last letter (< the 250 ms pause read)
        s.set(1, "");
        s.key(KC_RETURN, 0);
        s.wait(1000);
        assert_eq!(s.sent(), vec!["see you at noon"]);
    }

    #[test]
    fn paste_then_immediate_return_uses_the_fresh_read() {
        let mut s = Sim::new();
        s.key(9, 0x0010_0000); // Cmd+V
        s.set(1, "pasted from somewhere else entirely");
        s.key(KC_RETURN, 0); // before any scheduled read
        s.wait(100);
        s.set(1, "");
        s.wait(1000);
        assert_eq!(s.sent(), vec!["pasted from somewhere else entirely"]);
    }

    #[test]
    fn newline_return_is_not_captured() {
        let mut s = Sim::new();
        s.type_text("first line of a longer note");
        s.key(KC_RETURN, FLAG_SHIFT); // Shift+Return: newline, an edit
        s.set(1, "first line of a longer note\n");
        s.wait(300);
        // a browser textarea: bare Return also inserts a newline; the field doesn't clear
        s.key(KC_RETURN, 0);
        s.set(1, "first line of a longer note\n\n");
        s.wait(1500);
        assert!(s.sent().is_empty());
        assert_eq!(s.skips(), vec![Skip::NotConfirmed]);
    }

    #[test]
    fn field_that_clears_late_is_confirmed_by_the_second_check() {
        let mut s = Sim::new();
        s.type_text("on my way now");
        s.key(KC_RETURN, 0);
        s.wait(500); // first check (250 ms): still there
        assert!(s.sent().is_empty());
        s.set(1, "");
        s.wait(600);
        assert_eq!(s.sent(), vec!["on my way now"]);
    }

    #[test]
    fn return_accepting_a_mention_is_not_a_send() {
        let mut s = Sim::new();
        s.type_text("thanks @jo");
        s.key(KC_RETURN, 0);
        s.set(1, "thanks @John Smith ");
        s.wait(1500);
        assert!(s.sent().is_empty());
        s.type_text("for the help");
        s.key(KC_RETURN, 0);
        s.set(1, "");
        s.wait(1000);
        assert_eq!(s.sent(), vec!["thanks @John Smith for the help"]);
    }

    #[test]
    fn app_switch_right_after_a_newline_is_not_a_send() {
        let mut s = Sim::new();
        s.type_text("half written thought that");
        s.key(KC_RETURN, 0); // newline in this field
        s.set(1, "half written thought that\n");
        // Cmd+Tab to Messages within 100 ms: focus leaves the element, its text stays
        s.app(MESSAGES, 200, false);
        s.el(2, 200, "AXTextField", "");
        s.focus(Some(2));
        s.wait(1500);
        assert!(s.sent().is_empty());
    }

    #[test]
    fn element_that_disappears_counts_as_sent() {
        // Mail: Cmd+Shift+D closes the compose window, the body element is gone
        let mut s = Sim::new();
        s.type_text("hi all see attached");
        s.key(KC_RETURN, 0x0010_0000); // Cmd+Return
        s.st().values.remove(&1);
        s.focus(None);
        s.wait(400);
        assert_eq!(s.sent(), vec!["hi all see attached"]);
    }

    #[test]
    fn secure_input_and_password_fields_are_never_read() {
        let mut s = Sim::new();
        s.st().secure = true;
        s.type_text("hunter2 is my password");
        s.key(KC_RETURN, 0);
        s.set(1, "");
        s.wait(1000);
        assert!(s.sent().is_empty());
        assert_eq!(s.st().reads, 0);
        assert_eq!(s.skips(), vec![Skip::SecureInput]);

        let mut s = Sim::new();
        s.el(3, 100, "AXTextField", "AXSecureTextField");
        s.focus(Some(3));
        s.type_text("correct horse battery staple");
        s.key(KC_RETURN, 0);
        s.set(3, "");
        s.wait(1000);
        assert!(s.sent().is_empty());
        assert_eq!(s.st().reads, 0);
        assert_eq!(s.skips(), vec![Skip::PasswordField]);
    }

    #[test]
    fn apps_not_allowlisted_are_ignored() {
        let mut s = Sim::new();
        s.app(TERMINAL, 300, false);
        s.el(4, 300, "AXTextArea", "");
        s.focus(Some(4));
        s.type_text("git commit -m wip");
        s.key(KC_RETURN, 0);
        s.set(4, "");
        s.wait(1000);
        assert!(s.sent().is_empty());
        assert_eq!(s.st().reads, 0);
        assert_eq!(s.skips(), vec![Skip::NotAllowlisted(TERMINAL.into())]);
    }

    #[test]
    fn search_box_in_a_web_content_app_is_not_a_message() {
        let mut s = Sim::new();
        s.app(SLACK, 101, true); // Electron
        s.el(5, 101, "AXTextField", ""); // the "Search" box
        s.focus(Some(5));
        s.type_text("budget review notes");
        s.key(KC_RETURN, 0);
        s.focus(None); // the search results page takes focus
        s.wait(1000);
        assert!(s.sent().is_empty());
        assert_eq!(s.skips(), vec![Skip::Role("AXTextField".into())]);
        // ...while its composer (AXTextArea) is
        s.el(6, 101, "AXTextArea", "");
        s.focus(Some(6));
        s.type_text("sounds good to me");
        s.key(KC_RETURN, 0);
        s.set(6, "");
        s.wait(1000);
        assert_eq!(s.sent(), vec!["sounds good to me"]);
    }

    #[test]
    fn duplicates_short_messages_and_slash_commands_are_skipped() {
        let mut s = Sim::new();
        for _ in 0..2 {
            s.type_text("lunch at noon?");
            s.key(KC_RETURN, 0);
            s.set(1, "");
            s.wait(1000);
        }
        s.type_text("ok cool");
        s.key(KC_RETURN, 0);
        s.set(1, "");
        s.wait(1000);
        s.type_text("/giphy happy dance now");
        s.key(KC_RETURN, 0);
        s.set(1, "");
        s.wait(1000);
        assert_eq!(s.sent(), vec!["lunch at noon?"]);
        assert_eq!(
            s.skips(),
            vec![Skip::Duplicate, Skip::TooShort, Skip::SlashCommand]
        );
    }

    #[test]
    fn after_inject_is_flagged() {
        let mut s = Sim::new();
        s.st().injected_in = Some(SLACK.into());
        s.type_text("here is the edited draft");
        s.key(KC_RETURN, 0);
        s.set(1, "");
        s.wait(1000);
        assert!(s.st().sent[0].after_inject);
    }

    #[test]
    fn turning_capture_off_before_confirmation_drops_the_send() {
        let mut s = Sim::new();
        s.type_text("this should not leave");
        s.key(KC_RETURN, 0);
        s.st().off = true;
        s.set(1, "");
        s.wait(1000);
        assert!(s.sent().is_empty());
        assert_eq!(s.skips(), vec![Skip::Off]);
    }

    #[test]
    fn reads_are_throttled_while_typing() {
        let mut s = Sim::new();
        // 20 keys 10 ms apart: one delayed read for the burst + one after the pause
        for i in 0..20 {
            s.key(KEY_A, 0);
            s.set(1, &"x".repeat(i + 1));
            s.wait(10);
        }
        s.wait(500);
        let reads = s.st().reads;
        assert!(reads <= 7, "{reads} reads for a 200 ms burst");
        assert!(reads >= 2);
    }

    #[test]
    fn unreadable_field_reports_why() {
        let mut s = Sim::new();
        s.focus(None); // e.g. Electron before its AX tree is built
        s.key(KC_RETURN, 0);
        assert_eq!(s.skips(), vec![Skip::NoFocusedElement]);
    }
}
