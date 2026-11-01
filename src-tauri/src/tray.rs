//! Menu-bar tray icon and its menu.

use std::sync::Mutex;
use std::sync::atomic::Ordering;

use serde::Serialize;
use tauri::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, Wry};

use crate::AppState;

/// Shown until the engine answers `GET /personas` (the built-ins); then the engine's list.
pub const PERSONAS: [&str; 4] = ["Default", "Boss", "Friend", "Investor"];
pub const EVENT_PERSONA_CHANGED: &str = "persona-changed";

const ID_OPEN: &str = "open";
const ID_PAUSE: &str = "pause";
const ID_LOCK: &str = "lock";
const ID_QUIT: &str = "quit";
const PERSONA_PREFIX: &str = "persona:";

/// Serialises Persona-submenu rebuilds (startup sync and UI refreshes can overlap). Never the
/// `AppState::tray` lock: the main thread takes that one from menu events.
static REBUILD: Mutex<()> = Mutex::new(());

/// macOS menus treat a single `&` as a mnemonic marker and hide it: "Mom & Dad" needs "&&".
pub fn menu_label(name: &str) -> String {
    name.replace('&', "&&")
}

pub struct TrayItems {
    pub personas: Vec<(String, CheckMenuItem<Wry>)>,
    pub persona_menu: Submenu<Wry>,
    pub pause: CheckMenuItem<Wry>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PersonaChanged {
    pub persona: String,
}

pub fn persona_from_menu_id(id: &str) -> Option<&str> {
    id.strip_prefix(PERSONA_PREFIX)
}

/// The names the Persona submenu shows: the engine's order, blanks and case-insensitive
/// duplicates dropped; the built-ins when the list is empty.
pub fn menu_personas(names: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for n in names {
        let n = n.trim();
        if !n.is_empty() && !out.iter().any(|o| o.eq_ignore_ascii_case(n)) {
            out.push(n.to_string());
        }
    }
    if out.is_empty() {
        out = PERSONAS.iter().map(|p| p.to_string()).collect();
    }
    out
}

fn persona_items(
    app: &AppHandle,
    names: &[String],
    active: &str,
) -> tauri::Result<Vec<(String, CheckMenuItem<Wry>)>> {
    names
        .iter()
        .map(|p| {
            CheckMenuItem::with_id(
                app,
                format!("{PERSONA_PREFIX}{p}"),
                menu_label(p),
                true,
                p.eq_ignore_ascii_case(active),
                None::<&str>,
            )
            .map(|item| (p.clone(), item))
        })
        .collect()
}

pub fn build(app: &AppHandle) -> tauri::Result<()> {
    let names = menu_personas(&[]);
    let persona_items = persona_items(app, &names, "Default")?;
    let persona_refs: Vec<&dyn tauri::menu::IsMenuItem<Wry>> = persona_items
        .iter()
        .map(|(_, i)| i as &dyn tauri::menu::IsMenuItem<Wry>)
        .collect();
    let persona_menu = Submenu::with_items(app, "Persona", true, &persona_refs)?;

    let open = MenuItem::with_id(app, ID_OPEN, "Open Polytone", true, None::<&str>)?;
    let pause = CheckMenuItem::with_id(app, ID_PAUSE, "Pause hotkey", true, false, None::<&str>)?;
    let lock = MenuItem::with_id(app, ID_LOCK, "Lock Polytone", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, ID_QUIT, "Quit", true, Some("CmdOrCtrl+Q"))?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;

    let menu = Menu::with_items(
        app,
        &[&persona_menu, &sep1, &open, &pause, &lock, &sep2, &quit],
    )?;

    let mut builder = TrayIconBuilder::with_id("polytone-tray")
        .menu(&menu)
        .tooltip("Polytone")
        .show_menu_on_left_click(true)
        .on_menu_event(on_menu_event);
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;

    *app.state::<AppState>().tray.lock().unwrap() = Some(TrayItems {
        personas: persona_items,
        persona_menu,
        pause,
    });
    Ok(())
}

/// Show `names` (the engine's persona list) in the Persona submenu, checking `active`. Menu
/// calls hop to the main thread, so they run with the tray lock released.
pub fn set_personas(app: &AppHandle, names: &[String], active: &str) {
    let names = menu_personas(names);
    let _rebuild = REBUILD.lock().unwrap_or_else(|e| e.into_inner());
    let state = app.state::<AppState>();
    let (menu, old) = {
        let guard = state.tray.lock().unwrap();
        let Some(items) = guard.as_ref() else { return };
        if items.personas.iter().map(|(n, _)| n).eq(names.iter()) {
            drop(guard);
            mark_persona(app, active);
            return;
        }
        (items.persona_menu.clone(), items.personas.clone())
    };
    let new = match persona_items(app, &names, active) {
        Ok(v) => v,
        Err(e) => {
            log::warn!("could not build the persona menu: {e}");
            return;
        }
    };
    for (_, item) in &old {
        let _ = menu.remove(item);
    }
    for (_, item) in &new {
        let _ = menu.append(item);
    }
    if let Some(items) = state.tray.lock().unwrap().as_mut() {
        items.personas = new;
    }
}

/// Re-read the engine's personas and active persona into the tray (startup, and after the UI
/// adds, rewords or deletes one).
pub async fn refresh_personas(app: &AppHandle) -> Result<Vec<String>, String> {
    let client = app.state::<AppState>().client.clone();
    let list = client.personas().await.map_err(|e| e.user_message())?;
    let active = client
        .get_config()
        .await
        .map(|c| c.active_persona)
        .unwrap_or_else(|_| "Default".into());
    let names: Vec<String> = list.into_iter().map(|p| p.name).collect();
    set_personas(app, &names, &active);
    Ok(menu_personas(&names))
}

/// UI command: the persona list changed; rebuild the tray menu. Returns the names shown.
#[tauri::command]
pub async fn refresh_tray_personas(app: AppHandle) -> Result<Vec<String>, String> {
    refresh_personas(&app).await
}

fn on_menu_event(app: &AppHandle, event: MenuEvent) {
    let id = event.id().as_ref();
    match id {
        ID_OPEN => show_main_window(app),
        ID_QUIT => app.exit(0),
        ID_LOCK => crate::security::lock_app(app, crate::security::LockReason::Tray),
        ID_PAUSE => {
            let paused = !app.state::<AppState>().paused.load(Ordering::SeqCst);
            set_paused(app, paused);
        }
        other => {
            if let Some(persona) = persona_from_menu_id(other) {
                select_persona(app, persona.to_string());
            }
        }
    }
}

pub fn show_main_window(app: &AppHandle) {
    // Undo an app-level hide (Test trigger hides the app to hand focus back).
    #[cfg(target_os = "macos")]
    {
        let _ = app.show();
    }
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

pub fn set_paused(app: &AppHandle, paused: bool) {
    let state = app.state::<AppState>();
    state.paused.store(paused, Ordering::SeqCst);
    // clone out, then call: menu calls off the main thread wait for it, and it may want this lock
    let pause = state.tray.lock().unwrap().as_ref().map(|i| i.pause.clone());
    if let Some(pause) = pause {
        let _ = pause.set_checked(paused);
    }
    log::info!("hotkey paused = {paused}");
}

/// Reflect `persona` in the check marks (no backend call).
pub fn mark_persona(app: &AppHandle, persona: &str) {
    let state = app.state::<AppState>();
    let items = state
        .tray
        .lock()
        .unwrap()
        .as_ref()
        .map(|i| i.personas.clone())
        .unwrap_or_default();
    for (name, item) in &items {
        let _ = item.set_checked(name.eq_ignore_ascii_case(persona));
    }
}

fn select_persona(app: &AppHandle, persona: String) {
    mark_persona(app, &persona);
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let client = app.state::<AppState>().client.clone();
        let p = persona.clone();
        match client.update_config(move |c| c.active_persona = p).await {
            Ok(cfg) => {
                mark_persona(&app, &cfg.active_persona);
                let _ = app.emit(
                    EVENT_PERSONA_CHANGED,
                    PersonaChanged {
                        persona: cfg.active_persona,
                    },
                );
            }
            Err(e) => {
                log::warn!("could not set persona {persona}: {e}");
                crate::notify(&app, &e.user_message());
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn persona_ids() {
        assert_eq!(persona_from_menu_id("persona:Boss"), Some("Boss"));
        assert_eq!(persona_from_menu_id("open"), None);
        assert!(PERSONAS.contains(&"Default"));
    }

    #[test]
    fn menu_names_keep_engine_order_and_fall_back_to_builtins() {
        let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        assert_eq!(
            menu_personas(&s(&["Default", "Boss", "Recruiter", " ", "recruiter", "Mom & Dad"])),
            s(&["Default", "Boss", "Recruiter", "Mom & Dad"])
        );
        assert_eq!(menu_personas(&[]), s(&PERSONAS));
        assert_eq!(menu_personas(&s(&["", "  "])), s(&PERSONAS));
    }

    #[test]
    fn ampersands_survive_the_menu_label() {
        assert_eq!(menu_label("Mom & Dad"), "Mom && Dad");
        assert_eq!(menu_label("Boss"), "Boss");
    }
}
