//! Menu-bar tray icon and its menu.

use std::sync::atomic::Ordering;

use serde::Serialize;
use tauri::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, Wry};

use crate::AppState;

pub const PERSONAS: [&str; 4] = ["Boss", "Friend", "Investor", "Default"];
pub const EVENT_PERSONA_CHANGED: &str = "persona-changed";

const ID_OPEN: &str = "open";
const ID_PAUSE: &str = "pause";
const ID_QUIT: &str = "quit";
const PERSONA_PREFIX: &str = "persona:";

pub struct TrayItems {
    pub personas: Vec<(String, CheckMenuItem<Wry>)>,
    pub pause: CheckMenuItem<Wry>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PersonaChanged {
    pub persona: String,
}

pub fn persona_from_menu_id(id: &str) -> Option<&str> {
    id.strip_prefix(PERSONA_PREFIX)
}

pub fn build(app: &AppHandle) -> tauri::Result<()> {
    let persona_items = PERSONAS
        .iter()
        .map(|p| {
            CheckMenuItem::with_id(
                app,
                format!("{PERSONA_PREFIX}{p}"),
                *p,
                true,
                *p == "Default",
                None::<&str>,
            )
            .map(|item| (p.to_string(), item))
        })
        .collect::<tauri::Result<Vec<_>>>()?;
    let persona_refs: Vec<&dyn tauri::menu::IsMenuItem<Wry>> = persona_items
        .iter()
        .map(|(_, i)| i as &dyn tauri::menu::IsMenuItem<Wry>)
        .collect();
    let persona_menu = Submenu::with_items(app, "Persona", true, &persona_refs)?;

    let open = MenuItem::with_id(app, ID_OPEN, "Open Polytone", true, None::<&str>)?;
    let pause = CheckMenuItem::with_id(app, ID_PAUSE, "Pause hotkey", true, false, None::<&str>)?;
    let quit = MenuItem::with_id(app, ID_QUIT, "Quit", true, Some("CmdOrCtrl+Q"))?;
    let sep1 = PredefinedMenuItem::separator(app)?;
    let sep2 = PredefinedMenuItem::separator(app)?;

    let menu = Menu::with_items(app, &[&persona_menu, &sep1, &open, &pause, &sep2, &quit])?;

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
        pause,
    });
    Ok(())
}

fn on_menu_event(app: &AppHandle, event: MenuEvent) {
    let id = event.id().as_ref();
    match id {
        ID_OPEN => show_main_window(app),
        ID_QUIT => app.exit(0),
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
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

pub fn set_paused(app: &AppHandle, paused: bool) {
    let state = app.state::<AppState>();
    state.paused.store(paused, Ordering::SeqCst);
    if let Some(items) = state.tray.lock().unwrap().as_ref() {
        let _ = items.pause.set_checked(paused);
    }
    log::info!("hotkey paused = {paused}");
}

/// Reflect `persona` in the check marks (no backend call).
pub fn mark_persona(app: &AppHandle, persona: &str) {
    let state = app.state::<AppState>();
    if let Some(items) = state.tray.lock().unwrap().as_ref() {
        for (name, item) in &items.personas {
            let _ = item.set_checked(name.eq_ignore_ascii_case(persona));
        }
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
}
