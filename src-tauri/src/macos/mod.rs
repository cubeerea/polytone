//! Every macOS C / Objective-C API Polytone calls, behind small safe wrappers.
//!
//! This is the only module that declares `extern "C"` blocks: CoreFoundation ([`cf`]),
//! Accessibility ([`ax`]), CoreGraphics events + Carbon secure input ([`events`]), and the
//! AppKit / Foundation / LocalAuthentication pieces used through `objc2` ([`apps`],
//! [`pasteboard`], [`local_auth`] for Touch ID, [`session_events`] for screen lock / sleep). Callers outside this module
//! should not need `unsafe`. [`keycodes`] is plain constants and compiles everywhere.

pub mod keycodes;

#[cfg(target_os = "macos")]
pub mod apps;
#[cfg(target_os = "macos")]
pub mod ax;
#[cfg(target_os = "macos")]
pub mod cf;
#[cfg(target_os = "macos")]
pub mod events;
#[cfg(target_os = "macos")]
pub mod local_auth;
#[cfg(target_os = "macos")]
pub mod pasteboard;
#[cfg(target_os = "macos")]
pub mod session_events;
