//! Step 2 of CONTRACTS.md "Hotkey loop": selection → whole field → original clipboard → empty.

use std::time::{Duration, Instant};

use serde::Serialize;

use super::clipboard::{ClipboardIo, SavedClipboard, restore_clipboard, save_clipboard};
use super::keys::KeySim;
use super::timing::{Phases, Timing, sleep};

/// Marker contained in every sentinel (see [`make_sentinel`]).
pub const SENTINEL_MARK: &str = "polytone-sentinel";

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

/// A clipboard value is usable intent if it is non-blank and not a sentinel — ours, or one left
/// behind by an earlier run (stale on the clipboard, or pasted into the field and copied back).
pub fn usable(sentinel: &str, value: Option<&str>) -> Option<String> {
    match value {
        Some(v) if v != sentinel && !v.contains(SENTINEL_MARK) && !v.trim().is_empty() => {
            Some(v.trim().to_string())
        }
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

/// After a ⌘C: wait until usable (non-sentinel) text is on the clipboard, or `timeout` (measured
/// from now) passes. While the clipboard's change count still equals `baseline` nothing has been
/// copied, so the text isn't read; the last check always reads it, whatever the count says.
fn wait_for_copy<C: ClipboardIo>(
    clip: &mut C,
    sentinel: &str,
    baseline: Option<i64>,
    timing: &Timing,
    timeout: Duration,
) -> Option<String> {
    let start = Instant::now();
    sleep(timing.copy_first_check.min(timeout));
    loop {
        let elapsed = start.elapsed();
        let last = elapsed >= timeout;
        let changed = match (baseline, clip.change_count()) {
            (Some(before), Some(now)) => now != before,
            _ => true,
        };
        if (changed || last)
            && let Some(v) = usable(sentinel, clip.get_text().as_deref())
        {
            return Some(v);
        }
        if last {
            return None;
        }
        sleep(timing.copy_poll.min(timeout - elapsed));
    }
}

/// Capture the user's intent. `selection_hint` is Accessibility's view of the focused field
/// (`Some(false)` = no selection) and only shortens how long we wait for the first ⌘C; it never
/// skips a step. Records `snapshot`, `copy` and (if reached) `select_all_copy` in `phases`.
///
/// On error the original clipboard is restored before returning.
pub fn capture<C: ClipboardIo, K: KeySim>(
    clip: &mut C,
    keys: &mut K,
    sentinel: &str,
    timing: &Timing,
    selection_hint: Option<bool>,
    phases: &mut Phases,
) -> Result<CaptureResult, String> {
    let saved = save_clipboard(clip);
    let original = saved.text();
    phases.mark("snapshot");
    let result = capture_inner(
        clip,
        keys,
        sentinel,
        timing,
        selection_hint,
        original.as_deref(),
        phases,
    );
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
    selection_hint: Option<bool>,
    original: Option<&str>,
    phases: &mut Phases,
) -> Result<CaptureOutcome, String> {
    clip.set_text(sentinel)?;
    sleep(timing.sentinel_settle);

    // 1. Selection.
    let baseline = clip.change_count();
    keys.copy()?;
    let timeout = if selection_hint == Some(false) {
        timing.selection_timeout_no_selection
    } else {
        timing.selection_timeout
    };
    let selection = wait_for_copy(clip, sentinel, baseline, timing, timeout);
    phases.mark("copy");
    if let Some(text) = selection {
        return Ok(CaptureOutcome::Captured {
            text,
            source: CaptureSource::Selection,
        });
    }

    // 2. Whole field.
    keys.select_all()?;
    sleep(timing.select_all_gap);
    let baseline = clip.change_count();
    keys.copy()?;
    let field = wait_for_copy(clip, sentinel, baseline, timing, timing.field_timeout);
    phases.mark("select_all_copy");
    if let Some(text) = field {
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
