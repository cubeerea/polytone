//! Capture / inject logic against a simulated app (focused field + clipboard).

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use super::clipboard::PLAIN_TEXT_UTI;
use super::run::wait_for_release;
use super::*;
use crate::backend_client::InjectMode;

/// Simulated focused text field + clipboard shared between the fakes.
#[derive(Default)]
struct World {
    clipboard: Option<String>,
    field: String,
    selection: Option<String>, // currently selected text
    log: Vec<String>,
    fail_copy: bool,
    /// If false, copy does nothing (app ignores Cmd+C, e.g. password field).
    copy_works: bool,
    /// Non-text clipboard content.
    image: Option<ImageSnapshot>,
    /// Opaque content we can't read at all (e.g. a file promise).
    opaque: bool,
    /// Simulate the macOS full-pasteboard snapshot.
    native: bool,
    native_restore_fails: bool,
    /// How long the "app" takes to put a copied selection on the clipboard.
    copy_delay: Duration,
    /// A copy that lands on the clipboard at the given time.
    pending: Option<(Instant, String)>,
    /// Pasteboard change count (bumped by every write); exposed when `counts` is set.
    changes: i64,
    counts: bool,
    /// The copy lands without bumping the change count (must still be seen by the final read).
    silent_copy: bool,
    text_reads: u32,
}

impl World {
    fn settle(&mut self) {
        if let Some((at, _)) = &self.pending
            && Instant::now() >= *at
        {
            let (_, text) = self.pending.take().unwrap();
            self.write(Some(text));
        }
    }

    fn write(&mut self, text: Option<String>) {
        self.clipboard = text;
        self.image = None;
        self.opaque = false;
        self.changes += 1;
    }
}

type W = Rc<RefCell<World>>;

struct FakeClip(W);
struct FakeKeys(W);

impl ClipboardIo for FakeClip {
    fn get_text(&mut self) -> Option<String> {
        let mut w = self.0.borrow_mut();
        w.settle();
        w.text_reads += 1;
        w.clipboard.clone().filter(|s| !s.is_empty())
    }
    fn set_text(&mut self, t: &str) -> Result<(), String> {
        let mut w = self.0.borrow_mut();
        w.log.push(format!("set:{t}"));
        w.write(Some(t.to_string()));
        Ok(())
    }
    fn clear(&mut self) -> Result<(), String> {
        let mut w = self.0.borrow_mut();
        w.log.push("clear".into());
        w.write(None);
        Ok(())
    }
    fn get_image(&mut self) -> Option<ImageSnapshot> {
        let w = self.0.borrow();
        w.image.clone().filter(|_| w.clipboard.is_none())
    }
    fn set_image(&mut self, img: &ImageSnapshot) -> Result<(), String> {
        let mut w = self.0.borrow_mut();
        w.log.push("set_image".into());
        w.write(None);
        w.image = Some(img.clone());
        Ok(())
    }
    fn save_native(&mut self) -> Option<Vec<NativeItem>> {
        let w = self.0.borrow();
        if !w.native {
            return None;
        }
        let mut reps: NativeItem = Vec::new();
        if let Some(t) = &w.clipboard {
            reps.push((PLAIN_TEXT_UTI.into(), t.clone().into_bytes()));
            reps.push(("public.rtf".into(), format!("{{\\rtf1 {t}}}").into_bytes()));
        }
        if let Some(img) = &w.image {
            reps.push(("public.png".into(), img.rgba.clone()));
        }
        Some(if reps.is_empty() { vec![] } else { vec![reps] })
    }
    fn restore_native(&mut self, items: &[NativeItem]) -> Result<(), String> {
        let mut w = self.0.borrow_mut();
        if w.native_restore_fails {
            return Err("nope".into());
        }
        w.log.push(format!(
            "restore_native:{}",
            items.iter().map(|i| i.len()).sum::<usize>()
        ));
        w.write(None);
        for (ty, data) in items.iter().flatten() {
            match ty.as_str() {
                PLAIN_TEXT_UTI => w.clipboard = Some(String::from_utf8(data.clone()).unwrap()),
                "public.png" => {
                    w.image = Some(ImageSnapshot {
                        width: 1,
                        height: 1,
                        rgba: data.clone(),
                    })
                }
                _ => {}
            }
        }
        Ok(())
    }
    fn has_content(&mut self) -> bool {
        let w = self.0.borrow();
        w.clipboard.is_some() || w.image.is_some() || w.opaque
    }
    fn change_count(&mut self) -> Option<i64> {
        let mut w = self.0.borrow_mut();
        w.settle();
        w.counts.then_some(w.changes)
    }
}

impl KeySim for FakeKeys {
    fn copy(&mut self) -> Result<(), String> {
        let mut w = self.0.borrow_mut();
        w.log.push("copy".into());
        if w.fail_copy {
            return Err("boom".into());
        }
        if !w.copy_works {
            return Ok(());
        }
        // Like real apps: copying an empty selection leaves clipboard untouched.
        if let Some(sel) = w.selection.clone().filter(|s| !s.is_empty()) {
            if w.silent_copy {
                w.clipboard = Some(sel);
            } else if w.copy_delay.is_zero() {
                w.write(Some(sel));
            } else {
                w.pending = Some((Instant::now() + w.copy_delay, sel));
            }
        }
        Ok(())
    }
    fn select_all(&mut self) -> Result<(), String> {
        let mut w = self.0.borrow_mut();
        w.log.push("select_all".into());
        w.selection = Some(w.field.clone());
        Ok(())
    }
    fn paste(&mut self) -> Result<(), String> {
        let mut w = self.0.borrow_mut();
        w.log.push("paste".into());
        let clip = w.clipboard.clone().unwrap_or_default();
        replace_selection(&mut w, &clip);
        Ok(())
    }
    fn type_text(&mut self, t: &str) -> Result<(), String> {
        let mut w = self.0.borrow_mut();
        w.log.push(format!("type:{t}"));
        replace_selection(&mut w, t);
        Ok(())
    }
}

fn replace_selection(w: &mut World, text: &str) {
    match w.selection.take() {
        Some(sel) if !sel.is_empty() => w.field = w.field.replacen(&sel, text, 1),
        _ => w.field.push_str(text),
    }
}

fn world(clipboard: Option<&str>, field: &str, selection: Option<&str>) -> W {
    Rc::new(RefCell::new(World {
        clipboard: clipboard.map(String::from),
        field: field.into(),
        selection: selection.map(String::from),
        copy_works: true,
        ..Default::default()
    }))
}

const S: &str = "<<polytone-sentinel>>";

fn run_capture_with(
    w: &W,
    timing: &Timing,
    hint: Option<bool>,
) -> (Result<CaptureResult, String>, Phases) {
    let mut phases = Phases::new();
    let r = capture(
        &mut FakeClip(w.clone()),
        &mut FakeKeys(w.clone()),
        S,
        timing,
        hint,
        &mut phases,
    );
    (r, phases)
}

fn run_capture(w: &W) -> Result<CaptureResult, String> {
    run_capture_with(w, &Timing::INSTANT, None).0
}

fn captured(text: &str, source: CaptureSource) -> CaptureOutcome {
    CaptureOutcome::Captured {
        text: text.into(),
        source,
    }
}

#[test]
fn usable_rejects_sentinel_and_blank() {
    assert_eq!(usable(S, None), None);
    assert_eq!(usable(S, Some(S)), None);
    assert_eq!(usable(S, Some("   \n")), None);
    assert_eq!(usable(S, Some("  hi  ")), Some("hi".into()));
    // a sentinel from an earlier run is not intent either
    assert_eq!(
        usable(S, Some("polytone-sentinel-1793414545740352000")),
        None
    );
}

#[test]
fn sentinel_is_unique_and_not_blank() {
    let a = make_sentinel();
    assert!(!a.trim().is_empty());
    assert!(a.contains("polytone-sentinel"));
}

#[test]
fn captures_selection_first() {
    let w = world(Some("orig"), "hello there friend", Some("there"));
    let r = run_capture(&w).unwrap();
    assert_eq!(r.outcome, captured("there", CaptureSource::Selection));
    assert_eq!(r.original.as_deref(), Some("orig"));
    assert!(!w.borrow().log.contains(&"select_all".to_string()));
}

#[test]
fn falls_back_to_whole_field() {
    let w = world(Some("orig"), "rough intent text", None);
    let r = run_capture(&w).unwrap();
    assert_eq!(
        r.outcome,
        captured("rough intent text", CaptureSource::Field)
    );
    let log = w.borrow().log.clone();
    assert_eq!(
        log,
        vec![
            format!("set:{S}"),
            "copy".into(),
            "select_all".into(),
            "copy".into()
        ]
    );
}

#[test]
fn falls_back_to_original_clipboard_when_field_empty() {
    let w = world(Some("from clipboard"), "", None);
    let r = run_capture(&w).unwrap();
    assert_eq!(
        r.outcome,
        captured("from clipboard", CaptureSource::Clipboard)
    );
}

#[test]
fn falls_back_to_clipboard_when_app_ignores_copy() {
    let w = world(Some("from clipboard"), "secret", None);
    w.borrow_mut().copy_works = false;
    let r = run_capture(&w).unwrap();
    assert_eq!(
        r.outcome,
        captured("from clipboard", CaptureSource::Clipboard)
    );
}

#[test]
fn empty_everywhere_is_empty() {
    let w = world(None, "", None);
    let r = run_capture(&w).unwrap();
    assert_eq!(r.outcome, CaptureOutcome::Empty);
    assert_eq!(r.original, None);

    let w = world(Some("   "), "  ", None);
    let r = run_capture(&w).unwrap();
    assert_eq!(r.outcome, CaptureOutcome::Empty);
}

#[test]
fn whitespace_selection_falls_through_to_field() {
    let w = world(None, "  real text ", Some("   "));
    let r = run_capture(&w).unwrap();
    assert_eq!(r.outcome, captured("real text", CaptureSource::Field));
}

#[test]
fn capture_error_restores_original_clipboard() {
    let w = world(Some("orig"), "x", None);
    w.borrow_mut().fail_copy = true;
    assert!(run_capture(&w).is_err());
    assert_eq!(w.borrow().clipboard.as_deref(), Some("orig"));
}

fn inject_into(w: &W, draft: &str, mode: InjectMode, saved: &SavedClipboard) {
    inject(
        &mut FakeClip(w.clone()),
        &mut FakeKeys(w.clone()),
        draft,
        mode,
        saved,
        &Timing::INSTANT,
    )
    .unwrap();
}

#[test]
fn paste_inject_replaces_field_and_restores_clipboard() {
    let w = world(Some("orig"), "rough intent", None);
    let r = run_capture(&w).unwrap();
    inject_into(&w, "Polished draft.", InjectMode::Paste, &r.saved);
    let w = w.borrow();
    assert_eq!(w.field, "Polished draft.");
    assert_eq!(w.clipboard.as_deref(), Some("orig"));
}

#[test]
fn paste_inject_replaces_only_selection() {
    let w = world(None, "Hi, rough bit. Bye", Some("rough bit"));
    let r = run_capture(&w).unwrap();
    inject_into(&w, "nice bit", InjectMode::Paste, &r.saved);
    let w = w.borrow();
    assert_eq!(w.field, "Hi, nice bit. Bye");
    // original clipboard was empty -> cleared afterwards
    assert_eq!(w.clipboard, None);
    assert_eq!(w.log.last().map(String::as_str), Some("clear"));
}

#[test]
fn type_inject_types_and_restores_clipboard() {
    let w = world(Some("orig"), "rough", None);
    let r = run_capture(&w).unwrap();
    inject_into(&w, "Typed draft", InjectMode::Type, &r.saved);
    let w = w.borrow();
    assert_eq!(w.field, "Typed draft");
    assert_eq!(w.clipboard.as_deref(), Some("orig"));
    assert!(!w.log.contains(&"paste".to_string()));
}

#[test]
fn split_inject_leaves_draft_on_clipboard_until_finished() {
    // run.rs reports the draft as visible after insert_draft, and restores afterwards.
    let w = world(Some("orig"), "rough intent", None);
    let r = run_capture(&w).unwrap();
    let mut clip = FakeClip(w.clone());
    insert_draft(
        &mut clip,
        &mut FakeKeys(w.clone()),
        "Draft.",
        InjectMode::Paste,
        &Timing::INSTANT,
    )
    .unwrap();
    assert_eq!(w.borrow().field, "Draft.");
    assert_eq!(w.borrow().clipboard.as_deref(), Some("Draft."));
    finish_inject(&mut clip, InjectMode::Paste, &r.saved, &Timing::INSTANT).unwrap();
    assert_eq!(w.borrow().clipboard.as_deref(), Some("orig"));
}

#[test]
fn finish_inject_waits_restore_delay_only_for_paste() {
    let timing = Timing {
        restore_delay: Duration::from_millis(40),
        ..Timing::INSTANT
    };
    let w = world(Some("orig"), "", None);
    let saved = SavedClipboard::Text("orig".into());
    let t = Instant::now();
    finish_inject(&mut FakeClip(w.clone()), InjectMode::Type, &saved, &timing).unwrap();
    assert!(t.elapsed() < Duration::from_millis(30));
    let t = Instant::now();
    finish_inject(&mut FakeClip(w.clone()), InjectMode::Paste, &saved, &timing).unwrap();
    assert!(t.elapsed() >= Duration::from_millis(40));
}

fn paste_run(w: &W, draft: &str) -> CaptureResult {
    let r = run_capture(w).unwrap();
    inject_into(w, draft, InjectMode::Paste, &r.saved);
    r
}

fn img() -> ImageSnapshot {
    ImageSnapshot {
        width: 1,
        height: 1,
        rgba: vec![1, 2, 3, 255],
    }
}

#[test]
fn image_clipboard_is_restored_not_cleared() {
    let w = world(None, "rough intent", None);
    w.borrow_mut().image = Some(img());
    let r = paste_run(&w, "Draft.");
    assert_eq!(r.saved, SavedClipboard::Image(img()));
    let w = w.borrow();
    assert_eq!(w.field, "Draft.");
    assert_eq!(w.image, Some(img()));
    assert_eq!(w.clipboard, None);
    assert!(!w.log.contains(&"clear".to_string()));
}

#[test]
fn native_snapshot_restores_every_representation() {
    let w = world(Some("orig"), "rough intent", None);
    w.borrow_mut().native = true;
    let r = paste_run(&w, "Draft.");
    assert!(matches!(&r.saved, SavedClipboard::Native(items) if items[0].len() == 2));
    assert_eq!(r.original.as_deref(), Some("orig"));
    let w = w.borrow();
    assert_eq!(w.field, "Draft.");
    assert_eq!(w.clipboard.as_deref(), Some("orig"));
    assert_eq!(w.log.last().map(String::as_str), Some("restore_native:2"));
}

#[test]
fn native_text_still_works_as_capture_fallback() {
    let w = world(Some("from clipboard"), "", None);
    w.borrow_mut().native = true;
    let r = run_capture(&w).unwrap();
    assert_eq!(
        r.outcome,
        captured("from clipboard", CaptureSource::Clipboard)
    );
}

#[test]
fn native_restore_failure_falls_back_to_text() {
    let w = world(Some("orig"), "rough", None);
    {
        let mut b = w.borrow_mut();
        b.native = true;
        b.native_restore_fails = true;
    }
    paste_run(&w, "Draft.");
    assert_eq!(w.borrow().clipboard.as_deref(), Some("orig"));
}

#[test]
fn unreadable_clipboard_is_not_clobbered_with_clear() {
    let w = world(None, "rough intent", None);
    w.borrow_mut().opaque = true;
    let r = paste_run(&w, "Draft.");
    assert_eq!(r.saved, SavedClipboard::Unrestorable);
    let w = w.borrow();
    assert_eq!(w.field, "Draft.");
    // We could not save it, so we leave the draft rather than wiping the clipboard.
    assert_eq!(w.clipboard.as_deref(), Some("Draft."));
    assert!(!w.log.contains(&"clear".to_string()));
}

#[test]
fn unreadable_clipboard_sentinel_is_removed_on_empty_capture() {
    let w = world(None, "", None);
    w.borrow_mut().opaque = true;
    let r = run_capture(&w).unwrap();
    assert_eq!(r.outcome, CaptureOutcome::Empty);
    restore_clipboard(&mut FakeClip(w.clone()), &r.saved).unwrap();
    assert_eq!(w.borrow().clipboard, None);
}

// ---------------------------------------------------------------------------
// Timing behavior (real sleeps, kept short)
// ---------------------------------------------------------------------------

/// Small, fast-polling timing for tests that need real waits.
const SHORT: Timing = Timing {
    copy_first_check: Duration::from_millis(1),
    copy_poll: Duration::from_millis(2),
    selection_timeout: Duration::from_millis(120),
    selection_timeout_no_selection: Duration::from_millis(30),
    field_timeout: Duration::from_millis(120),
    ..Timing::INSTANT
};

#[test]
fn slow_app_selection_is_still_captured_within_the_window() {
    // The app takes 80ms to copy: the poll keeps waiting (no fallback to ⌘A).
    let w = world(Some("orig"), "hello there friend", Some("there"));
    w.borrow_mut().copy_delay = Duration::from_millis(80);
    let (r, _) = run_capture_with(&w, &SHORT, None);
    assert_eq!(
        r.unwrap().outcome,
        captured("there", CaptureSource::Selection)
    );
    assert!(!w.borrow().log.contains(&"select_all".to_string()));
}

#[test]
fn copy_poll_returns_as_soon_as_the_text_lands() {
    let w = world(None, "hello there friend", Some("there"));
    w.borrow_mut().copy_delay = Duration::from_millis(10);
    let (r, phases) = run_capture_with(&w, &SHORT, None);
    assert_eq!(
        r.unwrap().outcome,
        captured("there", CaptureSource::Selection)
    );
    let copy = phases.get("copy").unwrap();
    assert!(copy >= Duration::from_millis(10));
    assert!(copy < Duration::from_millis(60), "{copy:?}"); // not the 120ms timeout
    assert_eq!(phases.names(), vec!["snapshot", "copy"]);
}

#[test]
fn no_selection_hint_only_shortens_the_wait() {
    let w = world(None, "rough intent text", None);
    let (r, phases) = run_capture_with(&w, &SHORT, Some(false));
    assert_eq!(
        r.unwrap().outcome,
        captured("rough intent text", CaptureSource::Field)
    );
    // ⌘C was still sent before ⌘A ⌘C.
    assert_eq!(w.borrow().log[1..], ["copy", "select_all", "copy"]);
    let copy = phases.get("copy").unwrap();
    assert!(
        copy >= Duration::from_millis(30) && copy < Duration::from_millis(100),
        "{copy:?}"
    );

    // A selection that lands within the short window is still the selection.
    let w = world(None, "hello there friend", Some("there"));
    w.borrow_mut().copy_delay = Duration::from_millis(5);
    let (r, _) = run_capture_with(&w, &SHORT, Some(false));
    assert_eq!(
        r.unwrap().outcome,
        captured("there", CaptureSource::Selection)
    );
}

#[test]
fn change_count_skips_text_reads_until_something_is_copied() {
    let w = world(None, "", None);
    w.borrow_mut().counts = true;
    let (r, _) = run_capture_with(&w, &SHORT, None);
    assert_eq!(r.unwrap().outcome, CaptureOutcome::Empty);
    // The snapshot, then one forced read at the end of each wait (selection + field) — none
    // while polling. Without a change count every poll reads the text.
    assert_eq!(w.borrow().text_reads, 3);
    let w2 = world(None, "", None);
    let _ = run_capture_with(&w2, &SHORT, None);
    assert!(w2.borrow().text_reads > 10, "{}", w2.borrow().text_reads);

    // And text that lands without a count change is still seen by the final read.
    let w = world(None, "hello", Some("hello"));
    {
        let mut b = w.borrow_mut();
        b.counts = true;
        b.silent_copy = true;
    }
    let (r, _) = run_capture_with(&w, &SHORT, None);
    assert_eq!(
        r.unwrap().outcome,
        captured("hello", CaptureSource::Selection)
    );
}

#[test]
fn release_wait_ends_when_keys_are_up() {
    let timing = Timing {
        release_poll: Duration::from_millis(1),
        release_timeout: Duration::from_millis(500),
        ..Timing::INSTANT
    };
    let t = Instant::now();
    let until = t + Duration::from_millis(20);
    wait_for_release(|| Instant::now() < until, || false, &timing);
    let e = t.elapsed();
    assert!(
        e >= Duration::from_millis(20) && e < Duration::from_millis(100),
        "{e:?}"
    );
    // stuck modifiers: gives up at the timeout
    let timing = Timing {
        release_timeout: Duration::from_millis(30),
        ..timing
    };
    let t = Instant::now();
    wait_for_release(|| false, || true, &timing);
    assert!(t.elapsed() < Duration::from_millis(150));
}

#[test]
fn loop_errors_have_user_messages() {
    use crate::backend_client::{BackendError, MSG_EMPTY, MSG_NOT_RUNNING};
    assert_eq!(LoopError::Empty.user_message(), MSG_EMPTY);
    assert_eq!(
        LoopError::Backend(BackendError::NotRunning).user_message(),
        MSG_NOT_RUNNING
    );
    assert!(
        LoopError::Inject("x".into())
            .user_message()
            .contains("could not insert")
    );
    assert!(
        LoopError::RestoreFailed("x".into())
            .user_message()
            .starts_with("Draft inserted")
    );
}

/// Before/after capture + inject timings against the simulated app (excludes the LLM call and
/// the hotkey release). Run with `cargo test --lib -- --ignored --nocapture bench_capture`.
#[test]
#[ignore]
fn bench_capture_legacy_vs_new() {
    // A typical native app answers ⌘C in ~5–20ms; use 15ms.
    let app_delay = Duration::from_millis(15);
    type Case<'a> = (
        &'a str,
        Option<&'a str>,
        &'a str,
        Option<&'a str>,
        Option<bool>,
    );
    let cases: [Case; 4] = [
        (
            "selection",
            Some("orig"),
            "hello there friend",
            Some("there"),
            Some(true),
        ),
        (
            "field (no AX hint)",
            Some("orig"),
            "rough intent text",
            None,
            None,
        ),
        (
            "field (AX: no selection)",
            Some("orig"),
            "rough intent text",
            None,
            Some(false),
        ),
        ("clipboard fallback", Some("from clipboard"), "", None, None),
    ];
    println!(
        "\n| path | legacy capture | new capture | legacy paste→visible | new paste→visible |"
    );
    println!("|---|---|---|---|---|");
    for (name, clip, field, sel, hint) in cases {
        let mut row = Vec::new();
        for timing in [Timing::LEGACY, Timing::MACOS] {
            let w = world(clip, field, sel);
            {
                let mut b = w.borrow_mut();
                b.copy_delay = app_delay;
                b.counts = true;
            }
            let (r, phases) = run_capture_with(
                &w,
                &timing,
                if timing == Timing::LEGACY { None } else { hint },
            );
            r.unwrap();
            let capture_time = phases.elapsed();
            let t = Instant::now();
            insert_draft(
                &mut FakeClip(w.clone()),
                &mut FakeKeys(w.clone()),
                "Draft.",
                InjectMode::Paste,
                &timing,
            )
            .unwrap();
            row.push((capture_time, t.elapsed()));
        }
        let ms = |d: Duration| format!("{:.0}ms", d.as_secs_f64() * 1000.0);
        println!(
            "| {name} | {} | {} | {} | {} |",
            ms(row[0].0),
            ms(row[1].0),
            ms(row[0].1),
            ms(row[1].1)
        );
    }
}

/// Touches the real macOS pasteboard (and puts the user's content back). Run with
/// `cargo test -- --ignored real_pasteboard`.
#[cfg(target_os = "macos")]
#[test]
#[ignore]
fn real_pasteboard_image_round_trip() {
    use super::clipboard::SystemClipboard;
    let mut clip = SystemClipboard::new().unwrap();
    let users = save_clipboard(&mut clip);
    let pixel = ImageSnapshot {
        width: 2,
        height: 1,
        rgba: vec![255, 0, 0, 255, 0, 0, 255, 255],
    };
    clip.set_image(&pixel).unwrap();
    let saved = save_clipboard(&mut clip);
    assert!(matches!(saved, SavedClipboard::Native(_)), "{saved:?}");
    let before = clip.change_count().unwrap();
    clip.set_text("draft text").unwrap();
    assert_ne!(clip.change_count().unwrap(), before);
    restore_clipboard(&mut clip, &saved).unwrap();
    let back = clip.get_image();
    let text = clip.get_text();
    restore_clipboard(&mut clip, &users).unwrap();
    assert_eq!(back.map(|i| (i.width, i.height)), Some((2, 1)));
    assert_eq!(text, None);
}

#[test]
fn parses_accelerators() {
    assert!(parse_accelerator(DEFAULT_HOTKEY).is_ok());
    assert!(parse_accelerator("Alt+Space").is_ok());
    assert!(parse_accelerator("NotAKey+++").is_err());
    assert!(parse_accelerator("").is_err());
}

/// Real per-call costs of the loop's OS pieces on this machine (no prompts, no keystrokes; the
/// pasteboard is snapshotted and restored). `cargo test --lib -- --ignored --nocapture bench_real`
#[cfg(target_os = "macos")]
#[test]
#[ignore]
fn bench_real_os_costs() {
    use super::clipboard::SystemClipboard;
    use super::keys::SystemKeys;
    fn time<T>(name: &str, n: u32, mut f: impl FnMut() -> T) {
        let _ = f(); // warm up
        let t = Instant::now();
        for _ in 0..n {
            std::hint::black_box(f());
        }
        println!(
            "{name}: {:.2}ms",
            t.elapsed().as_secs_f64() * 1000.0 / n as f64
        );
    }
    time(
        "frontmost_app (AX + NSRunningApplication)",
        20,
        crate::macos::apps::frontmost_app,
    );
    time("ax focused_selection hint", 20, || {
        crate::macos::ax::focused_selection(crate::macos::apps::frontmost_app().map(|a| a.pid))
    });
    time("arboard Clipboard::new", 20, || {
        SystemClipboard::new().is_ok()
    });
    time("Enigo::new (+drop)", 5, || SystemKeys::new().is_ok());
    let mut clip = SystemClipboard::new().unwrap();
    let users = save_clipboard(&mut clip);
    time("pasteboard change_count", 200, || clip.change_count());
    time("clipboard get_text", 200, || clip.get_text());
    time("full snapshot (save_clipboard)", 20, || {
        save_clipboard(&mut clip)
    });
    time("set_text", 20, || clip.set_text("polytone bench"));
    restore_clipboard(&mut clip, &users).unwrap();
}
