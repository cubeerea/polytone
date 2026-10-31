//! Global hotkey registration and the shortcut handler.

use std::sync::atomic::Ordering;

use tauri::{AppHandle, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutEvent, ShortcutState};

use crate::{AppState, notify};

use super::run;

pub const DEFAULT_HOTKEY: &str = "CmdOrCtrl+Shift+P";

/// Handler passed to the global-shortcut plugin builder (runs on the main thread: never blocks).
pub fn on_shortcut(app: &AppHandle, _shortcut: &Shortcut, event: ShortcutEvent) {
    let state = app.state::<AppState>();
    match event.state {
        ShortcutState::Pressed => {
            state.trigger_held.store(true, Ordering::SeqCst);
            if state.paused.load(Ordering::SeqCst) {
                return;
            }
            if run::request_run(app, None, false).is_err() {
                log::info!("hotkey ignored: loop already running");
            }
        }
        ShortcutState::Released => {
            state.trigger_held.store(false, Ordering::SeqCst);
        }
    }
}

pub fn parse_accelerator(accel: &str) -> Result<Shortcut, String> {
    accel
        .trim()
        .parse::<Shortcut>()
        .map_err(|e| format!("invalid hotkey '{accel}': {e}"))
}

/// Replace the registered hotkey. On failure the previous one stays active.
pub fn register_hotkey(app: &AppHandle, accel: &str) -> Result<(), String> {
    let new = parse_accelerator(accel)?;
    let state = app.state::<AppState>();
    let gs = app.global_shortcut();
    let old_accel = state.hotkey.lock().unwrap().clone();
    let old = parse_accelerator(&old_accel).ok();

    if let Some(old) = old {
        if old == new && gs.is_registered(new) {
            return Ok(());
        }
        if gs.is_registered(old) {
            let _ = gs.unregister(old);
        }
    }
    match gs.register(new) {
        Ok(()) => {
            *state.hotkey.lock().unwrap() = accel.trim().to_string();
            log::info!("hotkey registered: {accel}");
            Ok(())
        }
        Err(e) => {
            if let Some(old) = old {
                let _ = gs.register(old);
            }
            Err(format!("could not register hotkey '{accel}': {e}"))
        }
    }
}

/// Register whatever hotkey is currently in state (used at startup).
pub fn register_initial(app: &AppHandle) {
    let state = app.state::<AppState>();
    let accel = state.hotkey.lock().unwrap().clone();
    match parse_accelerator(&accel) {
        Ok(sc) => {
            if let Err(e) = app.global_shortcut().register(sc) {
                log::error!("could not register hotkey {accel}: {e}");
                notify(app, &format!("Polytone could not register hotkey {accel}"));
            }
        }
        Err(e) => log::error!("{e}"),
    }
}
