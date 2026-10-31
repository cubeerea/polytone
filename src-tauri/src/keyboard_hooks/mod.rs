//! Global hotkey + the capture → `POST /generate` → inject loop (CONTRACTS.md "Hotkey loop").
//!
//! - [`clipboard`]: clipboard access, full-fidelity snapshot / restore.
//! - [`keys`]: simulated ⌘C / ⌘A / ⌘V / typing.
//! - [`capture`]: sentinel-based capture (selection → field → original clipboard).
//! - [`inject`]: paste or type the draft, then restore the clipboard.
//! - [`timing`]: every delay in one place + per-phase instrumentation.
//! - [`run`]: the worker thread that runs the loop end to end.
//! - [`hotkey`], [`commands`]: registration and the Tauri commands.
//!
//! The decision logic (`capture`, `inject`) is written against the [`ClipboardIo`] and
//! [`KeySim`] traits so it is unit-tested with fakes (see `tests.rs`).

pub mod capture;
pub mod clipboard;
pub mod commands;
pub mod hotkey;
pub mod inject;
pub mod keys;
pub mod run;
pub mod timing;

#[cfg(test)]
mod tests;

pub use capture::{
    CaptureOutcome, CaptureResult, CaptureSource, SENTINEL_MARK, capture, make_sentinel, usable,
};
pub use clipboard::{
    ClipboardIo, ImageSnapshot, NativeItem, SavedClipboard, restore_clipboard, save_clipboard,
};
pub use hotkey::{
    DEFAULT_HOTKEY, on_shortcut, parse_accelerator, register_hotkey, register_initial,
};
pub use inject::{finish_inject, inject, insert_draft};
pub use keys::KeySim;
pub use run::{EVENT_FINISHED, EVENT_STARTED, LoopError, RunResult, start_worker};
pub use timing::{Phases, Timing};
