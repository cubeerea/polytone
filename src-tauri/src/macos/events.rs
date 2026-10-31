//! CoreGraphics keyboard events: modifier flags, Input Monitoring access, a listen-only keyDown
//! event tap ([`KeyTap`]), and Carbon's secure-input check.

use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};

use super::cf::{CFRelease, CFStringRef, CFTypeRef};

pub use super::keycodes::modifiers_held;

type CFMachPortRef = *mut c_void;
type CFRunLoopRef = *mut c_void;
type CFRunLoopSourceRef = *mut c_void;
type CGEventRef = *mut c_void;
type TapCallback = extern "C" fn(
    proxy: *mut c_void,
    ty: u32,
    event: CGEventRef,
    refcon: *mut c_void,
) -> CGEventRef;

const K_CG_SESSION_EVENT_TAP: u32 = 1;
const K_CG_HEAD_INSERT: u32 = 0;
const K_CG_LISTEN_ONLY: u32 = 1;
const K_CG_EVENT_KEY_DOWN: u32 = 10;
const K_CG_KEYBOARD_EVENT_KEYCODE: u32 = 9;
const K_TAP_DISABLED_BY_TIMEOUT: u32 = 0xFFFF_FFFE;
const K_TAP_DISABLED_BY_USER_INPUT: u32 = 0xFFFF_FFFF;
/// kCGEventSourceStateHIDSystemState: the physical keyboard.
const K_HID_SYSTEM_STATE: i32 = 1;

#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    static kCFRunLoopCommonModes: CFStringRef;
    static kCFRunLoopDefaultMode: CFStringRef;
    fn CFRunLoopGetCurrent() -> CFRunLoopRef;
    fn CFRunLoopAddSource(rl: CFRunLoopRef, src: CFRunLoopSourceRef, mode: CFStringRef);
    fn CFRunLoopRunInMode(mode: CFStringRef, seconds: f64, return_after_source: u8) -> i32;
    fn CFRunLoopStop(rl: CFRunLoopRef);
    fn CFMachPortCreateRunLoopSource(
        alloc: CFTypeRef,
        port: CFMachPortRef,
        order: isize,
    ) -> CFRunLoopSourceRef;
    fn CFMachPortInvalidate(port: CFMachPortRef);
}

#[link(name = "CoreGraphics", kind = "framework")]
unsafe extern "C" {
    fn CGEventSourceFlagsState(state_id: i32) -> u64;
    fn CGEventTapCreate(
        tap: u32,
        place: u32,
        options: u32,
        events: u64,
        callback: TapCallback,
        refcon: *mut c_void,
    ) -> CFMachPortRef;
    fn CGEventTapEnable(tap: CFMachPortRef, enable: bool);
    fn CGEventGetIntegerValueField(event: CGEventRef, field: u32) -> i64;
    fn CGEventGetFlags(event: CGEventRef) -> u64;
    fn CGPreflightListenEventAccess() -> bool;
    fn CGRequestListenEventAccess() -> bool;
}

#[link(name = "Carbon", kind = "framework")]
unsafe extern "C" {
    fn IsSecureEventInputEnabled() -> u8;
}

/// Modifier flags of the physical keyboard right now.
pub fn modifier_flags_now() -> u64 {
    // SAFETY: plain C function taking an enum value.
    unsafe { CGEventSourceFlagsState(K_HID_SYSTEM_STATE) }
}

/// Ask for Input Monitoring if it hasn't been granted (shows the system prompt once).
pub fn ensure_listen_access() {
    // SAFETY: status query / prompt request, no arguments.
    unsafe {
        if !CGPreflightListenEventAccess() {
            CGRequestListenEventAccess();
        }
    }
}

/// A password field (or similar) has turned on secure keyboard entry.
pub fn secure_input_enabled() -> bool {
    // SAFETY: no arguments.
    unsafe { IsSecureEventInputEnabled() != 0 }
}

/// A keyDown seen by the tap.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KeyDown {
    pub keycode: i64,
    pub flags: u64,
}

struct TapCtx {
    on_key: Box<dyn Fn(KeyDown)>,
    port: AtomicPtr<c_void>,
}

extern "C" fn tap_callback(
    _proxy: *mut c_void,
    ty: u32,
    event: CGEventRef,
    refcon: *mut c_void,
) -> CGEventRef {
    if refcon.is_null() {
        return event;
    }
    // SAFETY: refcon is the TapCtx owned by the KeyTap, alive while the tap exists.
    let ctx = unsafe { &*(refcon as *const TapCtx) };
    if ty == K_TAP_DISABLED_BY_TIMEOUT || ty == K_TAP_DISABLED_BY_USER_INPUT {
        let port = ctx.port.load(Ordering::SeqCst);
        if !port.is_null() {
            // SAFETY: port is our live event tap.
            unsafe { CGEventTapEnable(port, true) };
        }
        return event;
    }
    if ty == K_CG_EVENT_KEY_DOWN && !event.is_null() {
        // SAFETY: event is the live CGEvent handed to the callback.
        let key = unsafe {
            KeyDown {
                keycode: CGEventGetIntegerValueField(event, K_CG_KEYBOARD_EVENT_KEYCODE),
                flags: CGEventGetFlags(event),
            }
        };
        (ctx.on_key)(key);
    }
    event
}

/// `CFRunLoopRef` of a thread running a [`KeyTap`]; can be stopped from any thread.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RunLoopHandle(usize);

impl RunLoopHandle {
    pub fn stop(self) {
        // SAFETY: CFRunLoopStop is thread-safe; the handle is only published while the loop runs.
        unsafe { CFRunLoopStop(self.0 as CFRunLoopRef) };
    }
}

/// A listen-only session keyDown tap. Not `Send`: create, run and drop it on one thread.
pub struct KeyTap {
    port: CFMachPortRef,
    ctx: *mut TapCtx,
}

impl KeyTap {
    /// Create the tap. `on_key` runs on the tap's run-loop thread and must not block.
    /// `None` = macOS refused (Input Monitoring / Accessibility not granted).
    pub fn new(on_key: impl Fn(KeyDown) + 'static) -> Option<KeyTap> {
        let ctx = Box::into_raw(Box::new(TapCtx {
            on_key: Box::new(on_key),
            port: AtomicPtr::new(std::ptr::null_mut()),
        }));
        // SAFETY: ctx stays valid until KeyTap::drop, after the tap is invalidated.
        let port = unsafe {
            CGEventTapCreate(
                K_CG_SESSION_EVENT_TAP,
                K_CG_HEAD_INSERT,
                K_CG_LISTEN_ONLY,
                1u64 << K_CG_EVENT_KEY_DOWN,
                tap_callback,
                ctx.cast(),
            )
        };
        if port.is_null() {
            // SAFETY: the tap was never created, so nothing else references ctx.
            drop(unsafe { Box::from_raw(ctx) });
            return None;
        }
        // SAFETY: ctx is live.
        unsafe { (*ctx).port.store(port, Ordering::SeqCst) };
        Some(KeyTap { port, ctx })
    }

    /// Attach to this thread's run loop and run until `stop` is set (checked at least once a
    /// second, and right away after [`RunLoopHandle::stop`]). `started` gets the handle once the
    /// tap is live; `stopping` runs before teardown.
    pub fn run(
        &self,
        stop: &AtomicBool,
        started: impl FnOnce(RunLoopHandle),
        stopping: impl FnOnce(),
    ) {
        // SAFETY: port is a live tap; all run-loop calls happen on the current thread.
        unsafe {
            let src = CFMachPortCreateRunLoopSource(std::ptr::null(), self.port, 0);
            let rl = CFRunLoopGetCurrent();
            CFRunLoopAddSource(rl, src, kCFRunLoopCommonModes);
            CGEventTapEnable(self.port, true);
            started(RunLoopHandle(rl as usize));
            while !stop.load(Ordering::SeqCst) {
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, 0);
            }
            stopping();
            CGEventTapEnable(self.port, false);
            if !src.is_null() {
                CFRelease(src as CFTypeRef);
            }
        }
    }
}

impl Drop for KeyTap {
    fn drop(&mut self) {
        // SAFETY: we own the port (+1 from CGEventTapCreate) and ctx; invalidating first
        // guarantees the callback can't run with a dangling refcon.
        unsafe {
            CGEventTapEnable(self.port, false);
            CFMachPortInvalidate(self.port);
            CFRelease(self.port as CFTypeRef);
            drop(Box::from_raw(self.ctx));
        }
    }
}

/// Synthetic keyboard input for the end-to-end capture test only.
#[cfg(test)]
pub mod synth {
    use std::ffi::c_void;

    #[link(name = "CoreGraphics", kind = "framework")]
    unsafe extern "C" {
        fn CGEventCreateKeyboardEvent(src: *const c_void, key: u16, down: bool) -> *mut c_void;
        fn CGEventKeyboardSetUnicodeString(ev: *mut c_void, len: usize, s: *const u16);
        fn CGEventSetFlags(ev: *mut c_void, flags: u64);
        fn CGEventPost(tap: u32, ev: *mut c_void);
    }

    /// Post a key down + up at the HID level (seen by session taps, delivered to the
    /// frontmost app). `text` (if any) is the typed character(s), like enigo's `text()`.
    pub fn key(keycode: u16, flags: u64, text: Option<&str>) {
        let utf16: Vec<u16> = text.unwrap_or("").encode_utf16().collect();
        for down in [true, false] {
            // SAFETY: plain CG calls on an event we own and release.
            unsafe {
                let ev = CGEventCreateKeyboardEvent(std::ptr::null(), keycode, down);
                if ev.is_null() {
                    continue;
                }
                CGEventSetFlags(ev, flags);
                if !utf16.is_empty() {
                    CGEventKeyboardSetUnicodeString(ev, utf16.len(), utf16.as_ptr());
                }
                CGEventPost(0, ev); // kCGHIDEventTap
                super::CFRelease(ev as super::CFTypeRef);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_queries_do_not_need_permission() {
        let _ = modifier_flags_now();
        let _ = secure_input_enabled();
    }
}
