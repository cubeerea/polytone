//! Lifecycle of the keyboard tap thread + its worker (macOS).

use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Sender};
use std::thread;
use std::time::{Duration, Instant};

use crate::macos::events::{self, KeyDown, KeyTap, RunLoopHandle};

use super::shared;
use super::worker::Worker;

/// Serializes start/stop so two `set_capture` calls can't create two taps.
static LIFECYCLE: Mutex<()> = Mutex::new(());
/// Set by `stop()`; the tap thread's run loop checks it at least once a second.
static STOP: AtomicBool = AtomicBool::new(false);
/// The tap thread's run loop while it runs.
static RUN_LOOP: Mutex<Option<RunLoopHandle>> = Mutex::new(None);

pub fn start() -> Result<(), String> {
    let _guard = LIFECYCLE.lock().unwrap();
    if shared().running.load(Ordering::SeqCst) {
        return Ok(());
    }
    STOP.store(false, Ordering::SeqCst);
    events::ensure_listen_access();
    let (tx, rx) = mpsc::channel::<KeyDown>();
    let (ready_tx, ready_rx) = mpsc::channel::<Result<(), String>>();
    thread::Builder::new()
        .name("polytone-capture-worker".into())
        .spawn(move || Worker::default().run(rx))
        .map_err(|e| e.to_string())?;
    thread::Builder::new()
        .name("polytone-capture-tap".into())
        .spawn(move || tap_thread(tx, ready_tx))
        .map_err(|e| e.to_string())?;
    match ready_rx.recv_timeout(Duration::from_secs(2)) {
        Ok(r) => r,
        Err(_) => Err("event tap did not start in time".into()),
    }
}

fn tap_thread(tx: Sender<KeyDown>, ready: Sender<Result<(), String>>) {
    // The callback holds the only Sender: dropping the tap ends the worker.
    let tap = KeyTap::new(move |key| {
        if shared().enabled.load(Ordering::Relaxed) {
            let _ = tx.send(key); // unbounded: never blocks
        }
    });
    let Some(tap) = tap else {
        let _ = ready.send(Err(
            "could not listen to the keyboard: grant Polytone Input Monitoring and \
             Accessibility (System Settings > Privacy & Security), then turn capture on again"
                .into(),
        ));
        return;
    };
    tap.run(
        &STOP,
        |rl| {
            *RUN_LOOP.lock().unwrap() = Some(rl);
            shared().running.store(true, Ordering::SeqCst);
            let _ = ready.send(Ok(()));
            log::info!("typing capture: keyboard tap running");
        },
        || {
            shared().running.store(false, Ordering::SeqCst);
            *RUN_LOOP.lock().unwrap() = None;
        },
    );
    drop(tap);
    log::info!("typing capture: keyboard tap stopped");
}

/// Stop the tap and wait (≤ 2s) for its thread to tear it down.
pub fn stop() {
    let _guard = LIFECYCLE.lock().unwrap();
    STOP.store(true, Ordering::SeqCst);
    {
        // Hold the lock while stopping: the tap thread clears the handle (under this lock)
        // before its run loop goes away, so the handle is never stale here.
        let rl = RUN_LOOP.lock().unwrap();
        if let Some(rl) = *rl {
            rl.stop();
        }
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while shared().running.load(Ordering::SeqCst) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
}
