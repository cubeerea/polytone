//! Clipboard access ([`ClipboardIo`]), full-fidelity snapshots ([`SavedClipboard`]) and restore.
//!
//! Before touching the clipboard we take a [`SavedClipboard`] snapshot and put it back
//! afterwards. On macOS that snapshot is every NSPasteboard item with every representation as
//! raw bytes (plain + rich text, images, file URLs, app-private types), so images and copied
//! files survive a run. Elsewhere — or if the macOS snapshot is unavailable/too large — it falls
//! back to text, then an arboard RGBA image. Anything still unreadable is
//! [`SavedClipboard::Unrestorable`]: we then leave the clipboard as-is (holding the draft)
//! instead of wiping it. Known limits: lazily-promised data (file promises) is only kept if the
//! source app hands it over when asked, and the snapshot is capped at [`MAX_SNAPSHOT_BYTES`].

use super::capture::SENTINEL_MARK;

/// Larger native snapshots are skipped (we fall back to text / image).
pub const MAX_SNAPSHOT_BYTES: usize = 64 * 1024 * 1024;
pub const PLAIN_TEXT_UTI: &str = "public.utf8-plain-text";

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
    /// A counter that changes on every clipboard write (NSPasteboard `changeCount`). Lets the
    /// ⌘C poll skip reading text while nothing has been copied. `None` = unsupported.
    fn change_count(&mut self) -> Option<i64> {
        None
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

/// The system clipboard: arboard for text/images, raw NSPasteboard on macOS.
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
        crate::macos::pasteboard::save(MAX_SNAPSHOT_BYTES)
    }
    #[cfg(target_os = "macos")]
    fn restore_native(&mut self, items: &[NativeItem]) -> Result<(), String> {
        crate::macos::pasteboard::restore(items)
    }
    #[cfg(target_os = "macos")]
    fn has_content(&mut self) -> bool {
        crate::macos::pasteboard::has_content()
    }
    #[cfg(target_os = "macos")]
    fn change_count(&mut self) -> Option<i64> {
        Some(crate::macos::pasteboard::change_count())
    }
}
