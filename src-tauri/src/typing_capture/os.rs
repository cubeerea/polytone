//! [`CaptureOs`] on macOS: Accessibility reads, the frontmost app, secure input, and the
//! worker loop fed by the keyboard tap.

use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use objc2::rc::autoreleasepool;

use crate::backend_client::TypedIngestRequest;
use crate::macos::apps::frontmost_app;
use crate::macos::ax::{Element, is_process_trusted};
use crate::macos::events::{KeyDown, secure_input_enabled};

use super::policy::{Skip, needs_enhanced_ui};
use super::worker::{AppInfo, CaptureOs, FocusedRaw, Front, Worker};

/// Bounds how long a hung app can stall one AX query.
const AX_TIMEOUT_S: f32 = 0.25;

/// The app-wide hooks (capture state, allowlist, dispatch) are closures so the end-to-end
/// test can run the real Accessibility + tap code against its own probe app.
pub struct MacOs {
    pub frontmost: Box<dyn Fn() -> Option<Front>>,
    pub active: Box<dyn Fn() -> Result<(), Skip>>,
    pub allowlisted: Box<dyn Fn(&str) -> bool>,
    pub after_inject: Box<dyn Fn(&str) -> bool>,
    pub dispatch: Box<dyn Fn(TypedIngestRequest)>,
    pub skipped: SkipHook,
}

pub type SkipHook = Box<dyn Fn(&str, &Skip)>;

impl CaptureOs for MacOs {
    type El = Element;

    fn active(&self) -> Result<(), Skip> {
        (self.active)()
    }

    fn secure_input(&self) -> bool {
        secure_input_enabled()
    }

    fn frontmost(&self) -> Option<Front> {
        (self.frontmost)()
    }

    fn allowlisted(&self, bundle_id: &str) -> bool {
        (self.allowlisted)(bundle_id)
    }

    /// Electron apps (Slack, Discord, ...) build their AX tree only for clients that set
    /// `AXManualAccessibility`; Chromium browsers only for `AXEnhancedUserInterface` (what
    /// VoiceOver sets). Without this their message boxes have no readable AXValue.
    fn prepare(&self, front: &Front) -> AppInfo {
        let Some(app) = Element::application(front.pid) else {
            return AppInfo::default();
        };
        app.set_timeout(AX_TIMEOUT_S);
        let electron = app.has_attr("AXManualAccessibility");
        if electron {
            let ok = app.set_bool("AXManualAccessibility", true);
            log::debug!(
                "typing capture: {} is Electron; AXManualAccessibility set={ok}",
                front.bundle_id
            );
        }
        if needs_enhanced_ui(&front.bundle_id)
            && app.bool_attr("AXEnhancedUserInterface") != Some(true)
        {
            let ok = app.set_bool("AXEnhancedUserInterface", true);
            log::debug!(
                "typing capture: {} is Chromium; AXEnhancedUserInterface set={ok}",
                front.bundle_id
            );
        }
        AppInfo {
            web_content: electron,
        }
    }

    /// Asks the app itself first: the system-wide element fails outright on some systems
    /// (kAXErrorCannotComplete on macOS 26), the app element doesn't.
    fn focused(&self, pid: i32) -> Result<FocusedRaw<Element>, Skip> {
        let el = Element::focused_in(pid, AX_TIMEOUT_S).or_else(|| Element::focused(AX_TIMEOUT_S));
        let Some(el) = el else {
            return Err(if is_process_trusted() {
                Skip::NoFocusedElement
            } else {
                Skip::NoAccessibility
            });
        };
        el.set_timeout(AX_TIMEOUT_S);
        Ok(FocusedRaw {
            pid: el.pid(),
            role: el.string_attr("AXRole").unwrap_or_default(),
            subrole: el.string_attr("AXSubrole").unwrap_or_default(),
            element: el,
        })
    }

    fn value(&self, el: &Element) -> Option<String> {
        el.string_attr("AXValue")
    }

    fn after_inject(&self, bundle_id: &str) -> bool {
        (self.after_inject)(bundle_id)
    }

    fn dispatch(&self, req: TypedIngestRequest) {
        (self.dispatch)(req)
    }

    fn skipped(&self, bundle_id: &str, why: &Skip) {
        (self.skipped)(bundle_id, why)
    }
}

/// The app keystrokes go to.
pub fn front_app() -> Option<Front> {
    frontmost_app().map(|f| Front {
        bundle_id: f.bundle_id,
        name: f.name,
        pid: f.pid,
    })
}

/// Run `worker` until the tap (the channel's only sender) goes away.
pub fn run(mut worker: Worker<MacOs>, rx: Receiver<KeyDown>) {
    loop {
        let wait = worker.next_due().map_or(Duration::from_secs(3600), |t| {
            t.saturating_duration_since(Instant::now())
        });
        match rx.recv_timeout(wait) {
            Ok(key) => autoreleasepool(|_| worker.on_key(key.keycode, key.flags, Instant::now())),
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        autoreleasepool(|_| worker.tick(Instant::now()));
    }
}

#[cfg(test)]
mod tests {
    //! Real end-to-end: the probe app (`tests/capture_probe`), the real keyboard tap, real
    //! Accessibility reads and synthetic keystrokes. Needs Accessibility + Input Monitoring for
    //! the test binary's parent (e.g. the terminal), activates the probe window for a few
    //! seconds and aborts as soon as anything else takes focus:
    //! `cargo test -- --ignored --nocapture capture_e2e`
    use super::*;
    use crate::macos::events::{KeyTap, synth};
    use crate::typing_capture::policy::{FLAG_SHIFT, KC_RETURN};
    use std::process::Command;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex, mpsc};
    use std::thread;

    const PROBE: &str = "dev.polytone.captureprobe";
    const KC_SPACE: u16 = 49;

    /// The probe app, launched through LaunchServices (`open`) so macOS lets it activate.
    struct Probe(std::path::PathBuf);
    impl Drop for Probe {
        fn drop(&mut self) {
            let exe = self.0.join("Contents/MacOS/CaptureProbe");
            let _ = Command::new("pkill").arg("-f").arg(exe).status();
        }
    }

    static PROBE_PID: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(0);

    /// The probe, if it is the active app. Asks the probe itself (`AXFrontmost`): a test
    /// process has no main run loop, so NSWorkspace's frontmost app never updates in it.
    fn probe_front() -> Option<Front> {
        let pid = PROBE_PID.load(Ordering::SeqCst);
        let app = Element::application(pid).filter(|_| pid > 0)?;
        app.set_timeout(AX_TIMEOUT_S);
        (app.bool_attr("AXFrontmost") == Some(true)).then(|| Front {
            bundle_id: PROBE.into(),
            name: Some("CaptureProbe".into()),
            pid,
        })
    }

    fn front_is_probe() -> bool {
        probe_front().is_some()
    }

    fn type_text(text: &str) {
        for ch in text.chars() {
            assert!(front_is_probe(), "focus left the probe app: aborting");
            let code = if ch == ' ' { KC_SPACE } else { 0 };
            synth::key(code, 0, Some(&ch.to_string()));
            thread::sleep(Duration::from_millis(35));
        }
    }

    fn press(keycode: i64, flags: u64) {
        assert!(front_is_probe(), "focus left the probe app: aborting");
        synth::key(keycode as u16, flags, None);
    }

    fn wait_for(pred: impl Fn() -> bool, ms: u64) -> bool {
        let end = Instant::now() + Duration::from_millis(ms);
        while Instant::now() < end {
            if pred() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        pred()
    }

    type Sent = Arc<Mutex<Vec<TypedIngestRequest>>>;

    #[test]
    #[ignore]
    fn capture_e2e() {
        if !is_process_trusted() {
            println!("skipped: this process is not trusted for Accessibility");
            return;
        }
        let dir = std::env::temp_dir().join(format!("polytone-capture-e2e-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let script = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/capture_probe/build.sh");
        let built = Command::new("sh").arg(script).arg(&dir).output().unwrap();
        assert!(
            built.status.success(),
            "{}",
            String::from_utf8_lossy(&built.stderr)
        );
        let log_path = dir.join("sent.log");
        let app = dir.join("CaptureProbe.app");
        let opened = Command::new("open")
            .arg("-n")
            .arg(&app)
            .arg("--env")
            .arg(format!("PROBE_LOG={}", log_path.display()))
            .arg("--env")
            .arg("PROBE_CLEAR_MS=60")
            .arg("--args")
            .arg("textview")
            .status()
            .unwrap();
        assert!(opened.success());
        let exe = app.join("Contents/MacOS/CaptureProbe");
        let _probe = Probe(app);
        assert!(wait_for(
            || {
                let out = Command::new("pgrep").arg("-f").arg(&exe).output().unwrap();
                let pid = String::from_utf8_lossy(&out.stdout)
                    .trim()
                    .parse()
                    .unwrap_or(0);
                PROBE_PID.store(pid, Ordering::SeqCst);
                pid > 0
            },
            5000
        ));
        assert!(
            wait_for(front_is_probe, 5000),
            "the probe app never became frontmost"
        );

        // the real tap → the real worker, with test hooks
        let sent: Sent = Arc::default();
        let skips: Arc<Mutex<Vec<String>>> = Arc::default();
        static STOP: AtomicBool = AtomicBool::new(false);
        let (tx, rx) = mpsc::channel::<KeyDown>();
        let (ready_tx, ready_rx) = mpsc::channel::<bool>();
        let (s2, k2) = (sent.clone(), skips.clone());
        let worker = thread::spawn(move || {
            let os = MacOs {
                frontmost: Box::new(probe_front),
                active: Box::new(|| Ok(())),
                allowlisted: Box::new(|b| b == PROBE),
                after_inject: Box::new(|_| false),
                dispatch: Box::new(move |r| s2.lock().unwrap().push(r)),
                skipped: Box::new(move |b, why| k2.lock().unwrap().push(format!("{b}: {why}"))),
            };
            run(Worker::new(os), rx)
        });
        let tap = thread::spawn(move || {
            let Some(tap) = KeyTap::new(move |k| {
                let _ = tx.send(k);
            }) else {
                let _ = ready_tx.send(false);
                return;
            };
            tap.run(
                &STOP,
                |_| {
                    let _ = ready_tx.send(true);
                },
                || {},
            );
        });
        if !ready_rx
            .recv_timeout(Duration::from_secs(2))
            .unwrap_or(false)
        {
            println!("skipped: could not create the keyboard tap (Input Monitoring)");
            return;
        }

        // 1) a message sent with Return; the probe clears its field 60 ms later
        type_text("hello there my friend");
        press(KC_RETURN, 0);
        assert!(
            wait_for(|| !sent.lock().unwrap().is_empty(), 2500),
            "nothing captured; skips: {:?}",
            skips.lock().unwrap()
        );
        // 2) Shift+Return is a newline; then the multi-line message is sent
        type_text("first line here");
        press(KC_RETURN, FLAG_SHIFT);
        thread::sleep(Duration::from_millis(400));
        type_text("and the second one");
        thread::sleep(Duration::from_millis(1200));
        assert_eq!(sent.lock().unwrap().len(), 1, "Shift+Return must not send");
        press(KC_RETURN, 0);
        assert!(wait_for(|| sent.lock().unwrap().len() == 2, 2500));
        // 3) too short: a skip, not a send
        type_text("ok");
        press(KC_RETURN, 0);
        thread::sleep(Duration::from_millis(1200));

        STOP.store(true, Ordering::SeqCst);
        let _ = tap.join();
        let _ = worker.join();
        let got: Vec<_> = sent
            .lock()
            .unwrap()
            .iter()
            .map(|r| (r.app.clone(), r.text.clone()))
            .collect();
        println!("captured: {got:?}\nskips: {:?}", skips.lock().unwrap());
        println!(
            "probe sent: {:?}",
            std::fs::read_to_string(&log_path).unwrap_or_default()
        );
        assert_eq!(
            got,
            vec![
                (PROBE.to_string(), "hello there my friend".to_string()),
                (
                    PROBE.to_string(),
                    "first line here\nand the second one".to_string()
                ),
            ]
        );
        assert!(
            skips
                .lock()
                .unwrap()
                .iter()
                .any(|s| s.contains("fewer than"))
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
