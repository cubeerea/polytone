// Capture probe: a tiny chat-like AppKit app used by the macOS end-to-end typing-capture test
// (`cargo test -- --ignored --nocapture capture_e2e`, see src-tauri/src/typing_capture/os.rs).
//
// argv[1] picks the focused field: `textview` (NSTextView → AXTextArea), `textfield`
// (NSTextField → AXTextField), `webdiv` (WKWebView contenteditable div) or `webbody`
// (WKWebView with an editable <body>, like Mail's compose body). $PROBE_BACKGROUND=1 → never
// activates itself (for read-only AX probing without stealing focus).
// In `textview` mode Return "sends" like Slack: the message is appended to $PROBE_LOG and the
// field is cleared asynchronously ($PROBE_CLEAR_MS, default 60 ms); Shift+Return inserts a newline.
import AppKit
import WebKit

final class Delegate: NSObject, NSApplicationDelegate, NSTextViewDelegate {
    var window: NSWindow!
    let mode: String
    let log = ProcessInfo.processInfo.environment["PROBE_LOG"]
    let clearMs = Int(ProcessInfo.processInfo.environment["PROBE_CLEAR_MS"] ?? "60") ?? 60

    init(mode: String) { self.mode = mode }

    func applicationDidFinishLaunching(_ n: Notification) {
        window = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 420, height: 200),
                          styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "Polytone capture probe"
        let frame = NSRect(x: 10, y: 10, width: 400, height: 180)
        switch mode {
        case "textfield":
            let f = NSTextField(frame: NSRect(x: 10, y: 80, width: 400, height: 24))
            window.contentView!.addSubview(f)
            window.makeKeyAndOrderFront(nil)
            window.makeFirstResponder(f)
        case "webdiv", "webbody":
            let web = WKWebView(frame: frame)
            let html = mode == "webdiv"
                ? "<div id=m contenteditable=true style='height:150px'>hello there</div><script>m.focus()</script>"
                : "<body contenteditable=true>hello there<script>document.body.focus()</script></body>"
            web.loadHTMLString(html, baseURL: nil)
            window.contentView!.addSubview(web)
            window.makeKeyAndOrderFront(nil)
            window.makeFirstResponder(web)
        default:
            let tv = NSTextView(frame: frame)
            tv.delegate = self
            window.contentView!.addSubview(tv)
            window.makeKeyAndOrderFront(nil)
            window.makeFirstResponder(tv)
        }
        if ProcessInfo.processInfo.environment["PROBE_BACKGROUND"] == nil { NSApp.activate(ignoringOtherApps: true) }
    }

    func textView(_ tv: NSTextView, doCommandBy sel: Selector) -> Bool {
        guard sel == #selector(NSResponder.insertNewline(_:)) else { return false }
        if NSEvent.modifierFlags.contains(.shift) { return false } // newline
        let text = tv.string
        if let log = log, let h = FileHandle(forWritingAtPath: log) ?? {
            FileManager.default.createFile(atPath: log, contents: nil); return FileHandle(forWritingAtPath: log)
        }() {
            h.seekToEndOfFile(); h.write((text.replacingOccurrences(of: "\n", with: "\\n") + "\n").data(using: .utf8)!)
            h.closeFile()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(clearMs)) { tv.string = "" }
        return true
    }
}

let mode = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "textview"
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let d = Delegate(mode: mode)
app.delegate = d
app.run()
