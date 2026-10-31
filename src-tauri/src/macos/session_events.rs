//! System events that should lock Polytone: screen lock, sleep, fast user switching.

use std::ptr::NonNull;
use std::sync::Arc;

use block2::RcBlock;
use objc2_app_kit::{
    NSWorkspace, NSWorkspaceSessionDidResignActiveNotification, NSWorkspaceWillSleepNotification,
};
use objc2_foundation::{NSDistributedNotificationCenter, NSNotification, NSString};

/// Why the system asked us to lock.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LockTrigger {
    /// `com.apple.screenIsLocked` (Ctrl-Cmd-Q, screen saver with password, lid close).
    ScreenLocked,
    /// `NSWorkspaceWillSleepNotification`.
    Sleep,
    /// `NSWorkspaceSessionDidResignActiveNotification` (switched to another user).
    SessionResigned,
}

/// Register observers for the app's lifetime. `on_trigger` runs on the main thread (the
/// notifications are delivered by its run loop), so it must return quickly.
///
/// Call once, from the main thread, before the event loop runs.
pub fn observe(on_trigger: impl Fn(LockTrigger) + Send + Sync + 'static) {
    let cb = Arc::new(on_trigger);
    let block = |t: LockTrigger| {
        let cb = cb.clone();
        RcBlock::new(move |_n: NonNull<NSNotification>| cb(t))
    };

    let dist = NSDistributedNotificationCenter::defaultCenter();
    let screen = NSString::from_str("com.apple.screenIsLocked");
    let b = block(LockTrigger::ScreenLocked);
    // SAFETY: object = nil, queue = nil (deliver on the posting/main thread); the block only
    // calls a Send + Sync closure. The observer token is kept forever (app lifetime).
    let token =
        unsafe { dist.addObserverForName_object_queue_usingBlock(Some(&screen), None, None, &b) };
    std::mem::forget(token);

    let ws = NSWorkspace::sharedWorkspace().notificationCenter();
    // SAFETY: AppKit's notification name constants are immutable statics.
    let names = unsafe {
        [
            (NSWorkspaceWillSleepNotification, LockTrigger::Sleep),
            (
                NSWorkspaceSessionDidResignActiveNotification,
                LockTrigger::SessionResigned,
            ),
        ]
    };
    for (name, trigger) in names {
        let b = block(trigger);
        // SAFETY: as above.
        let token =
            unsafe { ws.addObserverForName_object_queue_usingBlock(Some(name), None, None, &b) };
        std::mem::forget(token);
    }
}
