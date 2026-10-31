import { useState } from "react";
import { errorMessage } from "../api";
import { unlockWith } from "../lock";
import { native } from "../tauri";
import { LockIcon } from "./Icons";

/** Full-window lock: one button. Touch ID, or the Mac password when macOS offers it. */
export function LockScreen() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const unlock = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await native.unlock();
      unlockWith(r.token);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const cancelled = error?.startsWith("Unlock cancelled");

  return (
    <div className="fade-in flex h-full flex-col items-center justify-center px-6 pb-10 text-center" data-tauri-drag-region>
      <LockIcon width={22} height={22} className="text-text-3" />
      <p className="mt-3 font-medium text-text">Polytone is locked</p>
      <p className="mt-1 max-w-[300px] text-[12px] text-text-2">
        Your drafts and writing style stay hidden until you unlock.
      </p>
      <button type="button" className="btn btn-primary mt-4 !h-[28px] !px-4" disabled={busy} autoFocus onClick={() => void unlock()}>
        {busy ? "Waiting for Touch ID…" : "Unlock with Touch ID"}
      </button>
      <p className={`mt-2 min-h-[18px] max-w-[320px] text-[12px] ${cancelled ? "text-text-3" : "text-bad"}`} role="alert">
        {error}
      </p>
      <p className="mt-6 max-w-[300px] text-[11.5px] leading-snug text-text-3">
        You can use your Mac password instead. The shortcut keeps working while locked.
      </p>
    </div>
  );
}

/** Outside the app (plain browser): no data, just where to find it. */
export function BrowserOnly() {
  return (
    <div className="flex h-full flex-col items-center justify-center px-6 pb-10 text-center">
      <LockIcon width={22} height={22} className="text-text-3" />
      <p className="mt-3 font-medium text-text">Open Polytone from the menu bar to view your data</p>
      <p className="mt-1 max-w-[320px] text-[12px] text-text-2">
        Drafts and your writing style are only shown inside the app, after Touch ID.
      </p>
    </div>
  );
}
