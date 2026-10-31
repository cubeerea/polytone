//! Delays used by the loop ([`Timing`]) and per-phase instrumentation ([`Phases`]).

use std::fmt::Write as _;
use std::thread;
use std::time::{Duration, Instant};

/// Every wait in the capture / inject loop.
///
/// Waits that exist to let *another app* react (⌘C landing on the clipboard) are deadline polls:
/// they end as soon as the result is visible and only use the whole budget when nothing happens.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Timing {
    /// How often to re-check that the hotkey and modifiers are physically up.
    pub release_poll: Duration,
    /// Give up waiting for the release after this long.
    pub release_timeout: Duration,
    /// After the release: let the key-up events reach the target app before our ⌘C.
    pub release_settle: Duration,
    /// After writing the sentinel, before ⌘C.
    pub sentinel_settle: Duration,
    /// After ⌘C, before the first clipboard check.
    pub copy_first_check: Duration,
    /// Clipboard re-check interval after ⌘C.
    pub copy_poll: Duration,
    /// How long (from ⌘C) a selection may take to reach the clipboard.
    pub selection_timeout: Duration,
    /// Same, when Accessibility reports the focused field has no selection.
    pub selection_timeout_no_selection: Duration,
    /// Between ⌘A and ⌘C.
    pub select_all_gap: Duration,
    /// How long (from the second ⌘C) the whole field may take to reach the clipboard.
    pub field_timeout: Duration,
    /// After putting the draft on the clipboard, before ⌘V.
    pub paste_settle: Duration,
    /// After ⌘V, before restoring the clipboard: the target app reads it asynchronously.
    pub restore_delay: Duration,
}

impl Timing {
    /// Production timings (macOS).
    pub const MACOS: Timing = Timing {
        release_poll: Duration::from_millis(5),
        release_timeout: Duration::from_millis(1500),
        release_settle: Duration::from_millis(15),
        // Our pasteboard write is synchronous: it is complete when set_text returns.
        sentinel_settle: Duration::ZERO,
        copy_first_check: Duration::from_millis(5),
        copy_poll: Duration::from_millis(5),
        // ≥ the old fixed schedule (checks at 80/160/240ms), but returns as soon as text lands.
        selection_timeout: Duration::from_millis(250),
        selection_timeout_no_selection: Duration::from_millis(80),
        select_all_gap: Duration::from_millis(10),
        field_timeout: Duration::from_millis(300),
        paste_settle: Duration::ZERO,
        // CONTRACTS.md: restore "after ~300ms". Not on the user-visible path (see run.rs).
        restore_delay: Duration::from_millis(300),
    };

    /// The fixed schedule used before the refactor (80ms between every step, 3 checks 80ms
    /// apart after each ⌘C). Kept to benchmark against.
    #[cfg(test)]
    pub const LEGACY: Timing = Timing {
        release_poll: Duration::from_millis(10),
        release_timeout: Duration::from_millis(1500),
        release_settle: Duration::from_millis(80),
        sentinel_settle: Duration::from_millis(80),
        copy_first_check: Duration::from_millis(80),
        copy_poll: Duration::from_millis(80),
        selection_timeout: Duration::from_millis(240),
        selection_timeout_no_selection: Duration::from_millis(240),
        select_all_gap: Duration::from_millis(80),
        field_timeout: Duration::from_millis(240),
        paste_settle: Duration::from_millis(80),
        restore_delay: Duration::from_millis(300),
    };

    #[cfg(test)]
    pub const INSTANT: Timing = Timing {
        release_poll: Duration::ZERO,
        release_timeout: Duration::ZERO,
        release_settle: Duration::ZERO,
        sentinel_settle: Duration::ZERO,
        copy_first_check: Duration::ZERO,
        copy_poll: Duration::ZERO,
        selection_timeout: Duration::ZERO,
        selection_timeout_no_selection: Duration::ZERO,
        select_all_gap: Duration::ZERO,
        field_timeout: Duration::ZERO,
        paste_settle: Duration::ZERO,
        restore_delay: Duration::ZERO,
    };
}

pub fn sleep(d: Duration) {
    if !d.is_zero() {
        thread::sleep(d);
    }
}

/// Wall-clock time spent in each phase of one run, in order.
#[derive(Debug, Clone)]
pub struct Phases {
    start: Instant,
    last: Instant,
    items: Vec<(&'static str, Duration)>,
}

impl Default for Phases {
    fn default() -> Self {
        Self::new()
    }
}

impl Phases {
    pub fn new() -> Self {
        let now = Instant::now();
        Phases {
            start: now,
            last: now,
            items: Vec::with_capacity(12),
        }
    }

    /// Close the current phase as `name` (time since the previous mark).
    pub fn mark(&mut self, name: &'static str) {
        let now = Instant::now();
        self.items.push((name, now - self.last));
        self.last = now;
    }

    pub fn elapsed(&self) -> Duration {
        self.start.elapsed()
    }

    pub fn get(&self, name: &str) -> Option<Duration> {
        self.items.iter().find(|(n, _)| *n == name).map(|(_, d)| *d)
    }

    pub fn names(&self) -> Vec<&'static str> {
        self.items.iter().map(|(n, _)| *n).collect()
    }

    /// `release=12.0ms copy=31.4ms generate=812.0ms ... total=870.2ms`
    pub fn summary(&self) -> String {
        let mut s = String::new();
        for (name, d) in &self.items {
            let _ = write!(s, "{name}={:.1}ms ", d.as_secs_f64() * 1000.0);
        }
        let _ = write!(s, "total={:.1}ms", self.elapsed().as_secs_f64() * 1000.0);
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phases_record_in_order() {
        let mut p = Phases::new();
        p.mark("a");
        sleep(Duration::from_millis(2));
        p.mark("b");
        assert_eq!(p.names(), vec!["a", "b"]);
        assert!(p.get("b").unwrap() >= Duration::from_millis(2));
        assert!(p.get("missing").is_none());
        let s = p.summary();
        assert!(
            s.starts_with("a=") && s.contains(" b=") && s.contains("total="),
            "{s}"
        );
    }

    #[test]
    fn production_timing_is_never_shorter_than_legacy_where_apps_must_react() {
        // Capture reliability: the windows other apps get to answer ⌘C must not shrink.
        let (new, old) = (Timing::MACOS, Timing::LEGACY);
        assert!(new.selection_timeout >= old.selection_timeout);
        assert!(new.field_timeout >= old.field_timeout);
        assert!(new.restore_delay >= old.restore_delay);
        assert_eq!(new.release_timeout, old.release_timeout);
    }
}
