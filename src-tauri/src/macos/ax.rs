//! Accessibility (AXUIElement): process trust, focused element, attribute reads.

use std::ffi::c_void;

use super::cf::{CFGetTypeID, CFStringRef, CFTypeRef, Cf, kCFBooleanFalse, kCFBooleanTrue};

type AXUIElementRef = *const c_void;

#[link(name = "ApplicationServices", kind = "framework")]
unsafe extern "C" {
    fn AXIsProcessTrusted() -> bool;
    fn AXUIElementGetTypeID() -> usize;
    fn AXUIElementCreateSystemWide() -> AXUIElementRef;
    fn AXUIElementCreateApplication(pid: i32) -> AXUIElementRef;
    fn AXUIElementCopyAttributeValue(
        el: AXUIElementRef,
        attr: CFStringRef,
        out: *mut CFTypeRef,
    ) -> i32;
    fn AXUIElementSetAttributeValue(el: AXUIElementRef, attr: CFStringRef, value: CFTypeRef)
    -> i32;
    fn AXUIElementGetPid(el: AXUIElementRef, pid: *mut i32) -> i32;
    fn AXUIElementSetMessagingTimeout(el: AXUIElementRef, secs: f32) -> i32;
}

/// Whether this process is trusted for Accessibility (no prompt).
pub fn is_process_trusted() -> bool {
    // SAFETY: no arguments, no preconditions.
    unsafe { AXIsProcessTrusted() }
}

/// An owned AXUIElement.
#[derive(PartialEq)]
pub struct Element(Cf);

impl Element {
    /// The system-wide element, with `timeout_secs` bounding how long a hung app can stall
    /// queries made through it.
    pub fn system_wide(timeout_secs: f32) -> Option<Element> {
        // SAFETY: returns a +1 element (or null).
        let el = Element(unsafe { Cf::from_owned(AXUIElementCreateSystemWide())? });
        el.set_timeout(timeout_secs);
        Some(el)
    }

    pub fn application(pid: i32) -> Option<Element> {
        // SAFETY: returns a +1 element (or null).
        Some(Element(unsafe {
            Cf::from_owned(AXUIElementCreateApplication(pid))?
        }))
    }

    /// The UI element that has keyboard focus (any app).
    pub fn focused(timeout_secs: f32) -> Option<Element> {
        Element::system_wide(timeout_secs)?.element_attr("AXFocusedUIElement")
    }

    pub fn set_timeout(&self, secs: f32) {
        // SAFETY: live element.
        unsafe { AXUIElementSetMessagingTimeout(self.0.as_ptr(), secs) };
    }

    fn attr(&self, name: &str) -> Option<Cf> {
        let attr = Cf::string(name)?;
        let mut out: CFTypeRef = std::ptr::null();
        // SAFETY: live element and attribute name; `out` receives a +1 reference on success.
        let err =
            unsafe { AXUIElementCopyAttributeValue(self.0.as_ptr(), attr.as_ptr(), &mut out) };
        // SAFETY: on success `out` is owned by us.
        if err == 0 {
            unsafe { Cf::from_owned(out) }
        } else {
            None
        }
    }

    /// An attribute whose value is another element (e.g. `AXFocusedApplication`).
    pub fn element_attr(&self, name: &str) -> Option<Element> {
        let v = self.attr(name)?;
        // SAFETY: v is a live CF object.
        (unsafe { CFGetTypeID(v.as_ptr()) == AXUIElementGetTypeID() }).then_some(Element(v))
    }

    /// A string attribute (`AXRole`, `AXValue`, `AXSelectedText`, ...).
    pub fn string_attr(&self, name: &str) -> Option<String> {
        self.attr(name)?.to_rust_string()
    }

    pub fn pid(&self) -> Option<i32> {
        let mut pid = 0i32;
        // SAFETY: live element; pid is a valid out pointer.
        (unsafe { AXUIElementGetPid(self.0.as_ptr(), &mut pid) } == 0 && pid > 0).then_some(pid)
    }

    /// Set a boolean attribute; true on success.
    pub fn set_bool(&self, name: &str, value: bool) -> bool {
        let Some(attr) = Cf::string(name) else {
            return false;
        };
        // SAFETY: live element + attribute; the boolean constants are static CF objects.
        unsafe {
            let v = if value {
                kCFBooleanTrue
            } else {
                kCFBooleanFalse
            };
            AXUIElementSetAttributeValue(self.0.as_ptr(), attr.as_ptr(), v) == 0
        }
    }
}

/// pid of the app that receives keystrokes, per Accessibility (None without permission).
pub fn focused_app_pid() -> Option<i32> {
    Element::system_wide(0.25)?
        .element_attr("AXFocusedApplication")?
        .pid()
}

/// Roles whose selection state we trust for the capture hint.
fn is_text_role(role: &str) -> bool {
    matches!(
        role,
        "AXTextArea" | "AXTextField" | "AXComboBox" | "AXSearchField"
    )
}

/// Does the focused text field have a non-empty selection? `None` = unknown (no permission,
/// not a text element, or the app doesn't expose `AXSelectedText` — e.g. Chromium web content
/// before its accessibility tree is enabled). Bounded to ~100ms for a hung app.
pub fn focused_selection() -> Option<bool> {
    let el = Element::focused(0.1)?;
    let role = el.string_attr("AXRole")?;
    if !is_text_role(&role) || el.string_attr("AXSubrole").as_deref() == Some("AXSecureTextField") {
        return None;
    }
    el.string_attr("AXSelectedText").map(|s| !s.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_roles() {
        assert!(is_text_role("AXTextArea"));
        assert!(is_text_role("AXTextField"));
        assert!(!is_text_role("AXWebArea"));
        assert!(!is_text_role("AXButton"));
    }

    #[test]
    fn system_wide_element_exists() {
        // Creating the element needs no permission; queries through it may fail without one.
        assert!(Element::system_wide(0.1).is_some());
        let _ = focused_selection(); // must not panic or hang without Accessibility
    }
}
