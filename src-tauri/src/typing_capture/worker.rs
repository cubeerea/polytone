//! The capture worker (macOS): gates keys, reads the focused field over Accessibility, confirms
//! sends and dispatches them.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::Ordering;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use objc2::rc::autoreleasepool;

use crate::backend_client::TypedIngestRequest;
use crate::macos::apps::{FrontApp, frontmost_app};
use crate::macos::ax::Element;
use crate::macos::events::{KeyDown, secure_input_enabled};

use super::policy::{
    CONFIRM_DELAYS, DEBOUNCE, KeyAction, SentLog, classify_key, confirm_sent, pick_text,
    prepare_text, text_role_allowed,
};
use super::{after_inject, allowlisted, dispatch, paused, shared};

struct Focused {
    element: Element,
    value: Option<String>,
}

/// Read the focused text element of `front` (None for password fields / non-text / other pid).
fn read_focused(bundle_id: &str, pid: i32) -> Option<Focused> {
    let el = Element::focused(0.25)?;
    if el.pid() != Some(pid) {
        return None;
    }
    let subrole = el.string_attr("AXSubrole").unwrap_or_default();
    if subrole == "AXSecureTextField" || subrole == "AXSearchField" {
        return None;
    }
    let role = el.string_attr("AXRole").unwrap_or_default();
    if !text_role_allowed(&role, bundle_id) {
        return None;
    }
    let value = el.string_attr("AXValue");
    Some(Focused { element: el, value })
}

/// Electron / Chromium apps only build their AX tree for assistive clients that ask.
fn prepare_app(pid: i32) {
    if let Some(app) = Element::application(pid) {
        app.set_bool("AXManualAccessibility", true);
    }
}

struct Cached {
    text: String,
    element: Element,
}

struct Pending {
    bundle_id: String,
    app_name: Option<String>,
    pid: i32,
    element: Element,
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
pub(super) struct Worker {
    cache: HashMap<String, Cached>,
    read_due: Option<Instant>,
    pending: Vec<Pending>,
    sent: SentLog,
    prepared: HashSet<i32>,
}

impl Worker {
    pub(super) fn run(mut self, rx: Receiver<KeyDown>) {
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
                Ok(key) => autoreleasepool(|_| self.on_key(key)),
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => break,
            }
            autoreleasepool(|_| self.tick());
        }
    }

    /// Gate: capture on, not paused, no secure input, frontmost app allowlisted.
    fn gate(&mut self) -> Option<FrontApp> {
        if !shared().enabled.load(Ordering::SeqCst) || paused() {
            return None;
        }
        if secure_input_enabled() {
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

    fn on_key(&mut self, key: KeyDown) {
        let Some(front) = self.gate() else { return };
        match classify_key(key.keycode, key.flags, &front.bundle_id) {
            KeyAction::Edit => self.read_due = Some(Instant::now() + DEBOUNCE),
            KeyAction::Send => self.on_send(front),
        }
    }

    fn refresh_cache(&mut self) {
        let Some(front) = self.gate() else { return };
        match read_focused(&front.bundle_id, front.pid) {
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

    fn on_send(&mut self, front: FrontApp) {
        self.read_due = None;
        let cached = self.cache.remove(&front.bundle_id);
        let now = read_focused(&front.bundle_id, front.pid);
        let same_el = match (&cached, &now) {
            (Some(c), Some(n)) => c.element == n.element,
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
            bundle_id: front.bundle_id,
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
            let focused = read_focused(&p.bundle_id, p.pid);
            let same_el = focused.as_ref().is_some_and(|f| f.element == p.element);
            let value = focused.as_ref().and_then(|f| f.value.as_deref());
            if confirm_sent(&p.text, same_el, value) {
                self.send(p);
            } else if p.check + 1 < CONFIRM_DELAYS.len() {
                p.check += 1;
                self.pending.push(p);
            } else {
                log::debug!(
                    "typing capture: Return in {} did not send; ignored",
                    p.bundle_id
                );
            }
        }
    }

    fn send(&mut self, p: Pending) {
        if !shared().enabled.load(Ordering::SeqCst) || paused() {
            return;
        }
        if !self.sent.should_send(&p.bundle_id, &p.text) {
            return;
        }
        dispatch(TypedIngestRequest {
            text: p.text,
            app: p.bundle_id,
            app_name: p.app_name,
            after_inject: p.after_inject,
        });
    }
}
