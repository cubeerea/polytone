//! CoreFoundation: an owned CF reference ([`Cf`]) and string conversion.

use std::ffi::c_void;

pub type CFTypeRef = *const c_void;
pub type CFStringRef = *const c_void;

const UTF8: u32 = 0x0800_0100;

#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    pub(super) static kCFBooleanTrue: CFTypeRef;
    pub(super) static kCFBooleanFalse: CFTypeRef;
    pub(super) fn CFRelease(cf: CFTypeRef);
    fn CFEqual(a: CFTypeRef, b: CFTypeRef) -> u8;
    pub(super) fn CFGetTypeID(cf: CFTypeRef) -> usize;
    fn CFStringGetTypeID() -> usize;
    fn CFStringGetLength(s: CFStringRef) -> isize;
    fn CFStringGetMaximumSizeForEncoding(len: isize, enc: u32) -> isize;
    fn CFStringGetCString(s: CFStringRef, buf: *mut u8, size: isize, enc: u32) -> u8;
    fn CFStringCreateWithBytes(
        alloc: CFTypeRef,
        bytes: *const u8,
        len: isize,
        enc: u32,
        external: u8,
    ) -> CFStringRef;
}

/// An owned (+1) CoreFoundation object, released on drop.
pub struct Cf(CFTypeRef);

impl Cf {
    /// Take ownership of a +1 reference (from a `Create`/`Copy` function).
    ///
    /// # Safety
    /// `p` must be null or a CF object the caller owns one retain on.
    pub unsafe fn from_owned(p: CFTypeRef) -> Option<Self> {
        (!p.is_null()).then_some(Cf(p))
    }

    pub fn as_ptr(&self) -> CFTypeRef {
        self.0
    }

    /// A new CFString.
    pub fn string(s: &str) -> Option<Cf> {
        // SAFETY: valid UTF-8 bytes + length; returns a +1 CFString (or null).
        unsafe {
            Cf::from_owned(CFStringCreateWithBytes(
                std::ptr::null(),
                s.as_ptr(),
                s.len() as isize,
                UTF8,
                0,
            ))
        }
    }

    /// The value as a Rust string, if it is a CFString.
    pub fn to_rust_string(&self) -> Option<String> {
        // SAFETY: self.0 is a live CF object; its type is checked before use as a CFString.
        unsafe {
            if CFGetTypeID(self.0) != CFStringGetTypeID() {
                return None;
            }
            let len = CFStringGetLength(self.0);
            let cap = CFStringGetMaximumSizeForEncoding(len, UTF8) + 1;
            let mut buf = vec![0u8; cap.max(1) as usize];
            if CFStringGetCString(self.0, buf.as_mut_ptr(), cap, UTF8) == 0 {
                return None;
            }
            let n = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
            buf.truncate(n);
            String::from_utf8(buf).ok()
        }
    }
}

impl PartialEq for Cf {
    fn eq(&self, other: &Self) -> bool {
        // SAFETY: both are live CF objects.
        unsafe { CFEqual(self.0, other.0) != 0 }
    }
}

impl Drop for Cf {
    fn drop(&mut self) {
        // SAFETY: we own one retain on a non-null CF object.
        unsafe { CFRelease(self.0) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn string_round_trip_and_equality() {
        let a = Cf::string("héllo — wörld").unwrap();
        assert_eq!(a.to_rust_string().as_deref(), Some("héllo — wörld"));
        assert!(a == Cf::string("héllo — wörld").unwrap());
        assert!(a != Cf::string("other").unwrap());
        assert_eq!(
            Cf::string("").unwrap().to_rust_string().as_deref(),
            Some("")
        );
    }
}
