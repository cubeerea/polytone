import { errorMessage } from "../../api";
import { ONBOARDING_KEY, useAction, useLocalStorage } from "../../hooks";
import { native, type PermissionStatus } from "../../tauri";
import { StatusDot } from "../../components/bits";

/** First-launch guide shown while Accessibility is missing. */
export function PermissionsCard({ perm, onRecheck }: { perm: PermissionStatus; onRecheck: () => void }) {
  const [dismissed, setDismissed] = useLocalStorage(ONBOARDING_KEY, null);
  const host = perm.launched_from_other_app ? perm.grant_to : undefined;
  const open = useAction(native.openPermissionSettings);

  if (dismissed) {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-fill px-3 py-2 text-[12px]">
        <StatusDot state="down" />
        <span className="flex-1 text-text-2">
          {host ? `Accessibility is off for ${host}, so the shortcut can't paste.` : "Accessibility access is off, so the shortcut can't paste."}
        </span>
        <button type="button" className="btn btn-plain" onClick={() => setDismissed(null)}>
          Set up
        </button>
      </div>
    );
  }

  return (
    <section className="fade-in">
      <h2 className="text-[15px] font-semibold">Allow Polytone to paste for you</h2>
      <p className="mt-1 text-[13px] text-text-2">
        Polytone reads your selection and types the rewrite back, so macOS needs to trust it.
      </p>
      <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-[13px] text-text-2 marker:text-text-3">
        {host ? (
          <li>
            Polytone was started from <span className="text-text">{host}</span>, so macOS checks {host}&apos;s
            permission, not Polytone&apos;s. In{" "}
            <span className="text-text">Privacy &amp; Security → Accessibility</span>, switch{" "}
            <span className="text-text">{host}</span> on (add it with + if it isn&apos;t listed).
          </li>
        ) : (
          <li>
            In <span className="text-text">Privacy &amp; Security → Accessibility</span>, switch Polytone on.
          </li>
        )}
        <li>
          If the shortcut still does nothing, also allow it under{" "}
          <button type="button" className="btn-plain btn h-auto p-0 text-[13px]" onClick={() => void open.run("input_monitoring")}>
            Input Monitoring
          </button>
          .
        </li>
        <li>{host ? `Quit and reopen ${host}, then start Polytone again.` : "Come back here. You may need to relaunch Polytone."}</li>
      </ol>
      <div className="mt-3 flex items-center gap-2">
        <button type="button" className="btn btn-primary" onClick={() => void open.run("accessibility")}>
          Open System Settings
        </button>
        <button type="button" className="btn" onClick={onRecheck}>
          Check again
        </button>
        <button type="button" className="btn btn-plain ml-auto text-text-3" onClick={() => setDismissed("1")}>
          Later
        </button>
      </div>
      {open.error != null && <p className="mt-2 text-[12px] text-bad">{errorMessage(open.error)}</p>}
    </section>
  );
}
