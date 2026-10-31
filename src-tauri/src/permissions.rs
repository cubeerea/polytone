//! macOS privacy permissions (Accessibility / Input Monitoring).
//!
//! Simulating Cmd+C / Cmd+V requires the app to be trusted for Accessibility.
//! Input Monitoring is only needed by some macOS versions for global key state;
//! we surface a link to it so the user can grant it if key events are dropped.

use serde::Serialize;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PermissionStatus {
    pub accessibility: bool,
    pub platform: String,
    /// The app macOS checks the permission against. When Polytone is started from a terminal
    /// (`npm run tauri:dev` in Terminal, iTerm, VS Code, …) that is the terminal app, not
    /// Polytone, and that app is the one to switch on in System Settings.
    pub grant_to: String,
    /// True when `grant_to` is a launching app rather than Polytone itself.
    pub launched_from_other_app: bool,
}

pub const ACCESSIBILITY_URL: &str =
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility";
pub const INPUT_MONITORING_URL: &str =
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent";

pub const INPUT_MONITORING_NOTE: &str = "If Polytone's hotkey fires but copy/paste does nothing, \
also enable Polytone under System Settings > Privacy & Security > Input Monitoring.";

#[cfg(target_os = "macos")]
mod ffi {
    #[link(name = "ApplicationServices", kind = "framework")]
    unsafe extern "C" {
        pub fn AXIsProcessTrusted() -> bool;
    }
}

/// Whether this process is trusted for Accessibility (always true off macOS).
pub fn accessibility_trusted() -> bool {
    #[cfg(target_os = "macos")]
    {
        // SAFETY: AXIsProcessTrusted takes no arguments and has no preconditions.
        unsafe { ffi::AXIsProcessTrusted() }
    }
    #[cfg(not(target_os = "macos"))]
    {
        true
    }
}

pub fn status() -> PermissionStatus {
    let host = responsible_app();
    PermissionStatus {
        accessibility: accessibility_trusted(),
        platform: std::env::consts::OS.to_string(),
        launched_from_other_app: host.is_some(),
        grant_to: host.unwrap_or_else(|| "Polytone".to_string()),
    }
}

/// App bundle name from an executable path: the OUTERMOST `X.app` in it, so a helper inside
/// `/Applications/Foo.app/Contents/Frameworks/Foo Helper.app/...` maps to "Foo".
pub fn bundle_name(path: &str) -> Option<String> {
    let idx = path
        .find(".app/")
        .or_else(|| path.strip_suffix(".app").map(|p| p.len()))?;
    let name = path[..idx].rsplit('/').next()?;
    (!name.is_empty()).then(|| name.to_string())
}

/// Given the executable paths of our ancestors (parent first, launchd excluded), the app
/// macOS attributes our privacy permissions to: the outermost app bundle above us that isn't
/// Polytone. None when Polytone was launched directly (Finder, Dock, `open`).
pub fn responsible_from_chain(ancestors: &[String]) -> Option<String> {
    ancestors
        .iter()
        .rev()
        .filter_map(|p| bundle_name(p))
        .find(|name| !name.eq_ignore_ascii_case("polytone"))
}

/// Executable paths of this process's ancestors, parent first, stopping before launchd.
fn ancestor_paths() -> Vec<String> {
    let mut out = Vec::new();
    let mut pid = std::os::unix::process::parent_id();
    for _ in 0..32 {
        if pid <= 1 {
            break;
        }
        let Ok(o) = std::process::Command::new("ps")
            .args(["-o", "ppid=,comm=", "-p", &pid.to_string()])
            .output()
        else {
            break;
        };
        let line = String::from_utf8_lossy(&o.stdout).trim().to_string();
        let Some((ppid, comm)) = line.split_once(char::is_whitespace) else {
            break;
        };
        out.push(comm.trim().to_string());
        pid = ppid.trim().parse().unwrap_or(0);
    }
    out
}

fn responsible_app() -> Option<String> {
    static CACHE: std::sync::OnceLock<Option<String>> = std::sync::OnceLock::new();
    CACHE
        .get_or_init(|| responsible_from_chain(&ancestor_paths()))
        .clone()
}

/// What to tell the user when Accessibility is missing.
pub fn missing_accessibility_message() -> String {
    match responsible_app() {
        Some(host) => format!(
            "Polytone was started from {host}, so macOS checks {host}'s permission. \
             Turn on {host} in Privacy & Security > Accessibility, then quit and reopen {host}."
        ),
        None => "Polytone needs Accessibility permission (System Settings > Privacy & Security)"
            .to_string(),
    }
}

/// Map a permission kind from the UI to its System Settings deep link.
pub fn settings_url(kind: &str) -> Option<&'static str> {
    match kind.to_ascii_lowercase().replace(['-', ' '], "_").as_str() {
        "accessibility" => Some(ACCESSIBILITY_URL),
        "input_monitoring" | "inputmonitoring" | "listen_event" | "listenevent" => {
            Some(INPUT_MONITORING_URL)
        }
        _ => None,
    }
}

#[tauri::command]
pub fn check_permissions() -> PermissionStatus {
    status()
}

#[tauri::command]
pub fn open_permission_settings(kind: String) -> Result<(), String> {
    let url = settings_url(&kind).ok_or_else(|| format!("unknown permission kind: {kind}"))?;
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(url)
            .spawn()
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = url;
        Err("permission settings are only available on macOS".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_kinds() {
        assert_eq!(settings_url("accessibility"), Some(ACCESSIBILITY_URL));
        assert_eq!(settings_url("Accessibility"), Some(ACCESSIBILITY_URL));
        assert_eq!(settings_url("input_monitoring"), Some(INPUT_MONITORING_URL));
        assert_eq!(settings_url("input-monitoring"), Some(INPUT_MONITORING_URL));
        assert_eq!(settings_url("ListenEvent"), Some(INPUT_MONITORING_URL));
        assert_eq!(settings_url("camera"), None);
    }

    #[test]
    fn bundle_names() {
        assert_eq!(
            bundle_name(
                "/Applications/Antigravity IDE.app/Contents/Frameworks/Antigravity IDE Helper.app/Contents/MacOS/Antigravity IDE Helper"
            ),
            Some("Antigravity IDE".into())
        );
        assert_eq!(
            bundle_name("/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
            Some("Terminal".into())
        );
        assert_eq!(bundle_name("/bin/zsh"), None);
        assert_eq!(bundle_name("node"), None);
    }

    #[test]
    fn responsible_app_is_outermost_non_polytone_bundle() {
        let chain = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        // npm run tauri:dev inside an IDE terminal
        assert_eq!(
            responsible_from_chain(&chain(&[
                "node",
                "/bin/zsh",
                "/Applications/Antigravity IDE.app/Contents/Frameworks/Antigravity IDE Helper.app/Contents/MacOS/Antigravity IDE Helper",
                "/Applications/Antigravity IDE.app/Contents/MacOS/Electron",
            ])),
            Some("Antigravity IDE".into())
        );
        // opened from Finder: parent is launchd, nothing above us
        assert_eq!(responsible_from_chain(&[]), None);
        assert_eq!(
            responsible_from_chain(&chain(&[
                "/Applications/Polytone.app/Contents/MacOS/polytone"
            ])),
            None
        );
    }

    #[test]
    fn status_reports_platform() {
        let s = status();
        assert_eq!(s.platform, std::env::consts::OS);
    }
}
