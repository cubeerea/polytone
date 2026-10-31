//! Tauri commands for the hotkey (names and JSON shapes are consumed by `src/tauri.ts`).

use std::sync::atomic::Ordering;
use std::sync::mpsc;

use serde::Serialize;
use tauri::{AppHandle, Manager};

use crate::AppState;

use super::hotkey::register_hotkey;
use super::run::{self, RunResult};

#[derive(Debug, Clone, Serialize)]
pub struct HotkeyStatus {
    pub hotkey: String,
    pub paused: bool,
    pub last_run: Option<RunResult>,
}

pub fn hotkey_status(state: &AppState) -> HotkeyStatus {
    HotkeyStatus {
        hotkey: state.hotkey.lock().unwrap().clone(),
        paused: state.paused.load(Ordering::SeqCst),
        last_run: state.last_run.lock().unwrap().clone(),
    }
}

#[tauri::command]
pub async fn set_hotkey(app: AppHandle, accel: String) -> Result<HotkeyStatus, String> {
    register_hotkey(&app, &accel)?;
    let state = app.state::<AppState>();
    let saved = state.hotkey.lock().unwrap().clone();
    // Persist to the backend config (best-effort).
    if let Err(e) = state
        .client
        .update_config(|c| c.hotkey = saved.clone())
        .await
    {
        log::warn!("could not persist hotkey to backend: {e}");
    }
    Ok(hotkey_status(&state))
}

#[tauri::command]
pub fn get_hotkey_status(state: tauri::State<'_, AppState>) -> HotkeyStatus {
    hotkey_status(&state)
}

#[tauri::command]
pub fn set_hotkey_paused(app: AppHandle, paused: bool) -> HotkeyStatus {
    crate::tray::set_paused(&app, paused);
    hotkey_status(&app.state::<AppState>())
}

#[tauri::command]
pub async fn trigger_now(app: AppHandle) -> Result<RunResult, String> {
    // Invoked from Polytone's own window, which has focus: hide the app so macOS
    // re-activates the app the user was in; the worker waits for that, then runs.
    #[cfg(target_os = "macos")]
    {
        let _ = app.hide();
    }
    let (tx, rx) = mpsc::channel();
    if let Err(busy) = run::request_run(&app, Some(tx), true) {
        return Ok(busy);
    }
    tauri::async_runtime::spawn_blocking(move || rx.recv())
        .await
        .map_err(|e| e.to_string())?
        .map_err(|_| "the hotkey worker stopped".to_string())
}
