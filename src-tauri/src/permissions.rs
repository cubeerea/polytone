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
    PermissionStatus {
        accessibility: accessibility_trusted(),
        platform: std::env::consts::OS.to_string(),
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
    fn status_reports_platform() {
        let s = status();
        assert_eq!(s.platform, std::env::consts::OS);
    }
}
