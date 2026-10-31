//! Simulated keystrokes ([`KeySim`]) and the enigo-backed implementation.

use std::thread;
use std::time::Duration;

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

/// Gap between modifier-down, key and modifier-up. Events are delivered in order regardless;
/// the gap is for apps that sample modifier state when they handle the key (was 20ms).
const CHORD_GAP: Duration = Duration::from_millis(8);

/// Real keystrokes. Kept for the app's lifetime by the hotkey worker: creating an `Enigo` costs
/// a permission check + event source, and dropping one sleeps until its queued events flush.
pub struct SystemKeys(enigo::Enigo);

impl SystemKeys {
    pub fn new() -> Result<Self, String> {
        let settings = enigo::Settings {
            // The loop checks Accessibility itself and opens System Settings with a clear message.
            open_prompt_to_get_permissions: false,
            ..Default::default()
        };
        enigo::Enigo::new(&settings)
            .map(SystemKeys)
            .map_err(|e| format!("keyboard simulation unavailable: {e}"))
    }

    /// Modifier + letter chord. On macOS we use raw virtual keycodes, which avoids the
    /// keyboard-layout lookup (that API must run on the main thread).
    fn chord(&mut self, letter: char) -> Result<(), String> {
        use enigo::{Direction, Key, Keyboard};
        let err = |x: enigo::InputError| x.to_string();
        let e = &mut self.0;

        #[cfg(target_os = "macos")]
        let modifier = Key::Meta;
        #[cfg(not(target_os = "macos"))]
        let modifier = Key::Control;

        #[cfg(target_os = "macos")]
        let code: u16 = {
            use crate::macos::keycodes::{KC_A, KC_C, KC_V};
            match letter {
                'a' => KC_A,
                'c' => KC_C,
                'v' => KC_V,
                _ => return Err(format!("unsupported chord letter {letter}")),
            }
        };

        e.key(modifier, Direction::Press).map_err(err)?;
        thread::sleep(CHORD_GAP);
        #[cfg(target_os = "macos")]
        let r = e.raw(code, Direction::Click).map_err(err);
        #[cfg(not(target_os = "macos"))]
        let r = e.key(Key::Unicode(letter), Direction::Click).map_err(err);
        thread::sleep(CHORD_GAP);
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
