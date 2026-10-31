//! Step 4 of CONTRACTS.md "Hotkey loop": replace the (still selected) text with the draft, then
//! restore the original clipboard.

use crate::backend_client::InjectMode;

use super::clipboard::{ClipboardIo, SavedClipboard, restore_clipboard};
use super::keys::KeySim;
use super::timing::{Timing, sleep};

/// Put the draft into the focused field. In paste mode the clipboard now holds the draft and
/// must be restored ([`finish_inject`]) once the target app has read it.
pub fn insert_draft<C: ClipboardIo, K: KeySim>(
    clip: &mut C,
    keys: &mut K,
    draft: &str,
    mode: InjectMode,
    timing: &Timing,
) -> Result<(), String> {
    match mode {
        InjectMode::Paste => {
            clip.set_text(draft)?;
            sleep(timing.paste_settle);
            keys.paste()
        }
        InjectMode::Type => keys.type_text(draft),
    }
}

/// Wait for the target app to read a pasted draft (paste mode only), then restore the clipboard.
pub fn finish_inject<C: ClipboardIo>(
    clip: &mut C,
    mode: InjectMode,
    saved: &SavedClipboard,
    timing: &Timing,
) -> Result<(), String> {
    if mode == InjectMode::Paste {
        sleep(timing.restore_delay);
    }
    restore_clipboard(clip, saved)
}

/// [`insert_draft`] then [`finish_inject`]; if inserting fails the clipboard is restored at once.
pub fn inject<C: ClipboardIo, K: KeySim>(
    clip: &mut C,
    keys: &mut K,
    draft: &str,
    mode: InjectMode,
    saved: &SavedClipboard,
    timing: &Timing,
) -> Result<(), String> {
    match insert_draft(clip, keys, draft, mode, timing) {
        Ok(()) => finish_inject(clip, mode, saved, timing),
        Err(e) => {
            let _ = restore_clipboard(clip, saved);
            Err(e)
        }
    }
}
