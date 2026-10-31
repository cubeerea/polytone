//! Best-effort autostart of the Python backend (`uv run polytone-api`).

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use tauri::{AppHandle, Manager};

use crate::AppState;

/// Repo root: `$POLYTONE_REPO`, else (debug builds) `CARGO_MANIFEST_DIR/..`.
pub fn resolve_repo_root(env_repo: Option<String>, manifest_dir: Option<&str>) -> Option<PathBuf> {
    if let Some(r) = env_repo.filter(|r| !r.trim().is_empty()) {
        return Some(PathBuf::from(r));
    }
    manifest_dir.and_then(|m| Path::new(m).parent().map(Path::to_path_buf))
}

fn repo_root() -> Option<PathBuf> {
    let manifest = if cfg!(debug_assertions) {
        Some(env!("CARGO_MANIFEST_DIR"))
    } else {
        None
    };
    resolve_repo_root(std::env::var("POLYTONE_REPO").ok(), manifest)
}

/// GUI apps on macOS don't inherit the shell PATH, so check common install dirs.
pub fn uv_candidates(home: Option<&str>) -> Vec<PathBuf> {
    let mut v = vec![PathBuf::from("uv")];
    if let Some(h) = home {
        v.push(Path::new(h).join(".local/bin/uv"));
        v.push(Path::new(h).join(".cargo/bin/uv"));
    }
    v.push(PathBuf::from("/opt/homebrew/bin/uv"));
    v.push(PathBuf::from("/usr/local/bin/uv"));
    v
}

/// The backend's data dir: `$POLYTONE_DATA_DIR`, else `<repo>/data` (see `security`).
pub fn data_dir() -> Option<PathBuf> {
    crate::security::resolve_data_dir(
        std::env::var("POLYTONE_DATA_DIR").ok(),
        repo_root().as_deref(),
    )
}

/// Where the backend's output goes: the terminal under `pnpm tauri dev`, else
/// `<data_dir>/backend.log` (0600, in a 0700 dir; a Finder-launched app has no terminal, so
/// inherited output would vanish).
fn backend_output(data: &Path) -> (Stdio, Stdio, Option<PathBuf>) {
    use std::io::IsTerminal;
    if std::io::stderr().is_terminal() {
        return (Stdio::inherit(), Stdio::inherit(), None);
    }
    let path = data.join("backend.log");
    if let Err(e) = crate::security::ensure_private_dir(data) {
        log::warn!("cannot create {}: {e}", data.display());
    }
    match crate::security::open_private_append(&path) {
        Ok(f) => match f.try_clone() {
            Ok(f2) => (Stdio::from(f), Stdio::from(f2), Some(path)),
            Err(_) => (Stdio::from(f), Stdio::null(), Some(path)),
        },
        Err(e) => {
            log::warn!("cannot open {}: {e}", path.display());
            (Stdio::null(), Stdio::null(), None)
        }
    }
}

fn spawn_backend(root: &Path, data: &Path, token: &str) -> Option<Child> {
    let home = std::env::var("HOME").ok();
    for uv in uv_candidates(home.as_deref()) {
        let (out, err, log_path) = backend_output(data);
        let mut cmd = Command::new(&uv);
        cmd.args(["run", "polytone-api"])
            .current_dir(root)
            // The backend exits when this pid disappears, so a crashed or force-quit app
            // never leaves a backend behind serving stale code.
            .env("POLYTONE_PARENT_PID", std::process::id().to_string())
            // The app token (CONTRACTS.md "Security"); the backend also writes it to
            // `<data_dir>/.api_token` so later launches of the app can attach.
            .env("POLYTONE_API_TOKEN", token)
            .stdin(Stdio::null())
            .stdout(out)
            .stderr(err);
        if let Some(p) = &log_path {
            log::info!("backend output → {}", p.display());
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            // Own process group so we can kill uv *and* the python child.
            cmd.process_group(0);
        }
        match cmd.spawn() {
            Ok(child) => {
                log::info!(
                    "spawned backend: {} run polytone-api (pid {}) in {}",
                    uv.display(),
                    child.id(),
                    root.display()
                );
                return Some(child);
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => {
                log::warn!("failed to spawn {}: {e}", uv.display());
                return None;
            }
        }
    }
    log::warn!("could not find `uv`; start the backend manually with `uv run polytone-api`");
    None
}

/// On launch: if /health fails, spawn the backend with a fresh app token; else attach to the
/// running one with the token from its token file. Then sync config once healthy.
pub fn autostart(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let client = app.state::<AppState>().client.clone();
        let data = data_dir();
        // Known in both cases: a 401 re-reads it (another process may own the running backend).
        client.set_token_file(data.as_ref().map(|d| d.join(crate::security::TOKEN_FILE)));
        if client.health().await.is_ok() {
            if !client.load_token_file() {
                log::warn!(
                    "attached backend left no {} (engine without auth?); sending no app token",
                    crate::security::TOKEN_FILE
                );
            }
        } else {
            let token = crate::security::generate_token();
            client.set_token(Some(token.clone()));
            match (repo_root(), data.as_deref()) {
                (Some(root), Some(data)) => {
                    if let Some(child) = spawn_backend(&root, data, &token) {
                        *app.state::<AppState>().backend_child.lock().unwrap() = Some(child);
                    }
                }
                _ => log::warn!(
                    "backend not running and repo root unknown (set POLYTONE_REPO); skipping autostart"
                ),
            }
            // First start can be slow (uv sync, embedding model load), so wait up to ~3 min,
            // but stop early and say so if the process we spawned has already died.
            let mut healthy = false;
            for i in 0..360 {
                tokio::time::sleep(Duration::from_millis(500)).await;
                if client.health().await.is_ok() {
                    healthy = true;
                    break;
                }
                if let Some(code) = child_exit(&app) {
                    log::warn!("backend exited during startup ({code})");
                    crate::notify(
                        &app,
                        "Backend failed to start. See data/backend.log, or run `uv run polytone-api`.",
                    );
                    return;
                }
                if i == 60 {
                    log::info!("backend still starting…");
                }
            }
            if !healthy {
                log::warn!("backend still not healthy; hotkey will report 'backend not running'");
                crate::notify(&app, "Backend didn't come up. See data/backend.log.");
                return;
            }
        }
        log::info!("backend healthy");
        crate::sync_from_backend_config(&app).await;
    });
}

/// Exit status of the backend we spawned, if it has already exited.
fn child_exit(app: &AppHandle) -> Option<String> {
    let state = app.state::<AppState>();
    let mut guard = state.backend_child.lock().unwrap();
    let child = guard.as_mut()?;
    match child.try_wait() {
        Ok(Some(status)) => {
            guard.take();
            Some(status.to_string())
        }
        _ => None,
    }
}

/// Kill the spawned backend (and its process group) on exit.
pub fn shutdown(app: &AppHandle) {
    let child = app.state::<AppState>().backend_child.lock().unwrap().take();
    if let Some(mut child) = child {
        let pid = child.id();
        log::info!("stopping backend (pid {pid})");
        #[cfg(unix)]
        {
            let _ = Command::new("kill")
                .args(["-TERM", &format!("-{pid}")])
                .status();
            for _ in 0..20 {
                if let Ok(Some(_)) = child.try_wait() {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = Command::new("kill")
                .args(["-KILL", &format!("-{pid}")])
                .status();
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// SIGTERM / SIGINT / SIGHUP (e.g. `kill`, Ctrl-C under `pnpm tauri dev`) bypass Tauri's
/// exit events, which would orphan the backend we spawned. Route them through a clean exit.
#[cfg(unix)]
pub fn exit_on_signals(app: &AppHandle) {
    use signal_hook::consts::{SIGHUP, SIGINT, SIGTERM};
    let mut signals = match signal_hook::iterator::Signals::new([SIGTERM, SIGINT, SIGHUP]) {
        Ok(s) => s,
        Err(e) => {
            log::warn!("signal handler not installed: {e}");
            return;
        }
    };
    let app = app.clone();
    let spawned = std::thread::Builder::new()
        .name("polytone-signals".into())
        .spawn(move || {
            if let Some(sig) = signals.forever().next() {
                log::info!("received signal {sig}; shutting down");
                shutdown(&app);
                app.exit(0);
            }
        });
    if let Err(e) = spawned {
        log::warn!("signal thread not started: {e}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repo_root_prefers_env() {
        assert_eq!(
            resolve_repo_root(Some("/x/repo".into()), Some("/a/b/src-tauri")),
            Some(PathBuf::from("/x/repo"))
        );
    }

    #[test]
    fn repo_root_falls_back_to_manifest_parent() {
        assert_eq!(
            resolve_repo_root(None, Some("/a/b/src-tauri")),
            Some(PathBuf::from("/a/b"))
        );
        assert_eq!(
            resolve_repo_root(Some("  ".into()), Some("/a/b/src-tauri")),
            Some(PathBuf::from("/a/b"))
        );
        assert_eq!(resolve_repo_root(None, None), None);
    }

    #[test]
    fn uv_candidates_include_home_paths() {
        let c = uv_candidates(Some("/Users/me"));
        assert_eq!(c[0], PathBuf::from("uv"));
        assert!(c.contains(&PathBuf::from("/Users/me/.local/bin/uv")));
    }
}
