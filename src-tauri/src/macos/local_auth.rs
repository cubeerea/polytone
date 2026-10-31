//! LocalAuthentication: "is this the Mac's owner?" (Touch ID, or the login password as fallback).

use std::sync::mpsc;
use std::time::Duration;

use block2::RcBlock;
use objc2::MainThreadMarker;
use objc2::runtime::Bool;
use objc2_foundation::{NSError, NSString};
use objc2_local_authentication::{LAContext, LAPolicy};

/// `LAError` code (negative, see `LAError.h`) plus the system's own description.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthFailure {
    pub code: isize,
    pub description: String,
}

/// `LAErrorAppCancel`: used when we give up waiting and invalidate the context.
const APP_CANCEL: isize = -9;

fn failure(e: &NSError) -> AuthFailure {
    AuthFailure {
        code: e.code(),
        description: e.localizedDescription().to_string(),
    }
}

/// Ask for `LAPolicyDeviceOwnerAuthentication` and wait for the answer (up to `timeout`).
///
/// `evaluatePolicy` itself never blocks: its reply block runs on a private framework queue and
/// hands the result to this thread over a channel. This function *does* wait for it, so it must
/// run on a background thread; on the main thread it refuses instead of freezing the UI.
pub fn authenticate(reason: &str, timeout: Duration) -> Result<(), AuthFailure> {
    if MainThreadMarker::new().is_some() {
        return Err(AuthFailure {
            code: APP_CANCEL,
            description: "authenticate() called on the main thread".into(),
        });
    }
    let policy = LAPolicy::DeviceOwnerAuthentication;
    // SAFETY: plain NSObject init; the context lives on this thread until the reply arrives
    // (deallocating it early would cancel the evaluation).
    let ctx = unsafe { LAContext::new() };
    // SAFETY: valid policy constant; the error out-param is handled by the binding.
    if let Err(e) = unsafe { ctx.canEvaluatePolicy_error(policy) } {
        return Err(failure(&e));
    }
    let (tx, rx) = mpsc::sync_channel::<Result<(), AuthFailure>>(1);
    let reply = RcBlock::new(move |ok: Bool, err: *mut NSError| {
        let res = if ok.as_bool() {
            Ok(())
        } else {
            // SAFETY: LocalAuthentication passes nil or a valid NSError for the call's duration.
            match unsafe { err.as_ref() } {
                Some(e) => Err(failure(e)),
                None => Err(AuthFailure {
                    code: -1,
                    description: "authentication failed".into(),
                }),
            }
        };
        let _ = tx.try_send(res);
    });
    let reason = NSString::from_str(reason);
    // SAFETY: the block only touches a `SyncSender` (Send + Sync), so it may run on any queue.
    unsafe { ctx.evaluatePolicy_localizedReason_reply(policy, &reason, &reply) };
    let res = rx.recv_timeout(timeout).unwrap_or_else(|_| {
        // SAFETY: invalidating a live context cancels the prompt (reply gets AppCancel).
        unsafe { ctx.invalidate() };
        Err(AuthFailure {
            code: APP_CANCEL,
            description: "timed out waiting for authentication".into(),
        })
    });
    drop(ctx);
    res
}
