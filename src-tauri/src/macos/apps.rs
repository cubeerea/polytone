//! Running applications (NSWorkspace / NSRunningApplication).

use objc2::rc::autoreleasepool;
use objc2_app_kit::{NSApplicationActivationPolicy, NSRunningApplication, NSWorkspace};

use super::ax;

/// The app keystrokes go to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrontApp {
    pub bundle_id: String,
    pub name: Option<String>,
    pub pid: i32,
}

/// The Accessibility focused application (always current, even off the main thread), falling
/// back to `NSWorkspace.frontmostApplication`. Takes ~1ms; replaces an `osascript` round trip.
pub fn frontmost_app() -> Option<FrontApp> {
    autoreleasepool(|_| {
        let app = ax::focused_app_pid()
            .and_then(NSRunningApplication::runningApplicationWithProcessIdentifier)
            .or_else(|| NSWorkspace::sharedWorkspace().frontmostApplication())?;
        Some(FrontApp {
            bundle_id: app.bundleIdentifier()?.to_string(),
            name: app.localizedName().map(|n| n.to_string()),
            pid: app.processIdentifier(),
        })
    })
}

/// Regular (Dock) apps as `(bundle id, display name)`, deduplicated, sorted by name.
pub fn running_apps() -> Vec<(String, String)> {
    autoreleasepool(|_| {
        let ws = NSWorkspace::sharedWorkspace();
        let mut out: Vec<(String, String)> = Vec::new();
        for app in ws.runningApplications().iter() {
            if app.activationPolicy() != NSApplicationActivationPolicy::Regular {
                continue;
            }
            let Some(bid) = app.bundleIdentifier().map(|b| b.to_string()) else {
                continue;
            };
            if out.iter().any(|(b, _)| *b == bid) {
                continue;
            }
            let name = app
                .localizedName()
                .map_or_else(|| bid.clone(), |n| n.to_string());
            out.push((bid, name));
        }
        out.sort_by_key(|(_, name)| name.to_lowercase());
        out
    })
}
