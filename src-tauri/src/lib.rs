//! Polytone desktop shell: tray, global hotkey, capture/inject loop, backend autostart.

pub mod backend_client;
pub mod backend_process;
pub mod keyboard_hooks;
pub mod permissions;
pub mod tray;

use std::process::Child;
use std::sync::Mutex;
use std::sync::atomic::AtomicBool;

use tauri::{AppHandle, Manager, RunEvent, WindowEvent};
use tauri_plugin_notification::NotificationExt;

use backend_client::{BackendClient, InjectMode};
use keyboard_hooks::{DEFAULT_HOTKEY, RunResult};

pub struct AppState {
    pub client: BackendClient,
    pub hotkey: Mutex<String>,
    pub paused: AtomicBool,
    /// True between the hotkey's Pressed and Released events.
    pub trigger_held: AtomicBool,
    pub running: AtomicBool,
    pub last_run: Mutex<Option<RunResult>>,
    pub inject_mode: Mutex<InjectMode>,
    pub backend_child: Mutex<Option<Child>>,
    pub tray: Mutex<Option<tray::TrayItems>>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            client: BackendClient::default(),
            hotkey: Mutex::new(DEFAULT_HOTKEY.to_string()),
            paused: AtomicBool::new(false),
            trigger_held: AtomicBool::new(false),
            running: AtomicBool::new(false),
            last_run: Mutex::new(None),
            inject_mode: Mutex::new(InjectMode::Paste),
            backend_child: Mutex::new(None),
            tray: Mutex::new(None),
        }
    }
}

/// Show a user-visible notification (best-effort).
pub fn notify(app: &AppHandle, body: &str) {
    if let Err(e) = app
        .notification()
        .builder()
        .title("Polytone")
        .body(body)
        .show()
    {
        log::warn!("notification failed ({e}): {body}");
    }
}

/// Pull hotkey / persona / inject_mode from the backend config.
pub async fn sync_from_backend_config(app: &AppHandle) {
    let client = app.state::<AppState>().client.clone();
    match client.get_config().await {
        Ok(cfg) => {
            *app.state::<AppState>().inject_mode.lock().unwrap() = cfg.inject_mode.clone();
            tray::mark_persona(app, &cfg.active_persona);
            if !cfg.hotkey.trim().is_empty() {
                let handle = app.clone();
                let accel = cfg.hotkey.clone();
                // Shortcut registration must happen on the main thread on macOS.
                let _ = app.run_on_main_thread(move || {
                    if let Err(e) = keyboard_hooks::register_hotkey(&handle, &accel) {
                        log::warn!("backend hotkey not applied: {e}");
                    }
                });
            }
        }
        Err(e) => log::warn!("could not read backend config: {e}"),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let _ = env_logger::Builder::from_env(
        env_logger::Env::default().default_filter_or("polytone_lib=info"),
    )
    .try_init();

    let app = tauri::Builder::default()
        .manage(AppState::default())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(keyboard_hooks::on_shortcut)
                .build(),
        )
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            let handle = app.handle().clone();
            tray::build(&handle)?;
            keyboard_hooks::register_initial(&handle);
            backend_process::autostart(&handle);
            if !permissions::accessibility_trusted() {
                log::warn!("Accessibility permission not granted yet");
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event
                && window.label() == "main"
            {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            keyboard_hooks::set_hotkey,
            keyboard_hooks::get_hotkey_status,
            keyboard_hooks::set_hotkey_paused,
            keyboard_hooks::trigger_now,
            permissions::check_permissions,
            permissions::open_permission_settings,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| match event {
        // Keep running in the menu bar when the window is hidden/closed.
        RunEvent::ExitRequested { code: None, api, .. } => api.prevent_exit(),
        RunEvent::Exit => backend_process::shutdown(handle),
        _ => {}
    });
}
