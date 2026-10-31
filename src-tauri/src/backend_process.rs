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

fn spawn_backend(root: &Path) -> Option<Child> {
    let home = std::env::var("HOME").ok();
    for uv in uv_candidates(home.as_deref()) {
        let mut cmd = Command::new(&uv);
        cmd.args(["run", "polytone-api"])
            .current_dir(root)
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit());
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

/// On launch: if /health fails, spawn the backend, then sync config once healthy.
pub fn autostart(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let client = app.state::<AppState>().client.clone();
        if client.health().await.is_err() {
            match repo_root() {
                Some(root) => {
                    if let Some(child) = spawn_backend(&root) {
                        *app.state::<AppState>().backend_child.lock().unwrap() = Some(child);
                    }
                }
                None => log::warn!(
                    "backend not running and repo root unknown (set POLYTONE_REPO); skipping autostart"
                ),
            }
            // Wait up to ~30s for it to come up.
            let mut healthy = false;
            for _ in 0..60 {
                tokio_sleep(Duration::from_millis(500)).await;
                if client.health().await.is_ok() {
                    healthy = true;
                    break;
                }
            }
            if !healthy {
                log::warn!("backend still not healthy; hotkey will report 'backend not running'");
                return;
            }
        }
        log::info!("backend healthy");
        crate::sync_from_backend_config(&app).await;
    });
}

async fn tokio_sleep(d: Duration) {
    // Avoid a direct tokio dependency: sleep on a blocking thread.
    let _ = tauri::async_runtime::spawn_blocking(move || std::thread::sleep(d)).await;
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
