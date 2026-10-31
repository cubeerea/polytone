//! macOS virtual key codes (`kVK_*`) and `CGEventFlags` modifier bits. Plain constants, so pure
//! logic using them compiles (and is tested) everywhere.

pub const FLAG_SHIFT: u64 = 0x0002_0000;
pub const FLAG_CONTROL: u64 = 0x0004_0000;
pub const FLAG_ALT: u64 = 0x0008_0000;
pub const FLAG_COMMAND: u64 = 0x0010_0000;
/// Shift, Control, Option, Command.
pub const MODIFIER_MASK: u64 = FLAG_SHIFT | FLAG_CONTROL | FLAG_ALT | FLAG_COMMAND;

pub const KC_A: u16 = 0x00;
pub const KC_C: u16 = 0x08;
pub const KC_V: u16 = 0x09;
pub const KC_D: i64 = 2;
pub const KC_RETURN: i64 = 36;
pub const KC_KEYPAD_ENTER: i64 = 76;

pub fn modifiers_held(flags: u64) -> bool {
    flags & MODIFIER_MASK != 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modifier_mask() {
        assert!(!modifiers_held(0));
        assert!(!modifiers_held(0x100)); // non-coalesced/device bits only
        assert!(!modifiers_held(0x0001_0000)); // caps lock
        assert!(modifiers_held(FLAG_SHIFT));
        assert!(modifiers_held(FLAG_COMMAND));
        assert!(modifiers_held(0x0012_0000));
    }
}
