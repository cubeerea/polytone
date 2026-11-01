import { useState } from "react";
import { api, type Persona } from "../../api";
import { useAction } from "../../hooks";
import { invalidateQueries } from "../../query";
import { AFTER_PERSONA_CHANGE, patchConfigLocally, usePersonaList } from "../../queries";
import { native } from "../../tauri";
import { ErrorLine, Loading } from "../../components/bits";

const NAME_MAX = 30;
const DESCRIPTION_MAX = 300;

/** After any change: refetch what it touches and rebuild the tray's Persona menu. */
function changed() {
  invalidateQueries(AFTER_PERSONA_CHANGE);
  void native.refreshTrayPersonas();
}

const samplesLabel = (n: number) => (n === 1 ? "1 sample" : `${n} samples`);

export function PersonasGroup({ online }: { online: boolean }) {
  const list = usePersonaList();
  const [adding, setAdding] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const ps = list.data ?? [];
  // an engine that predates managing personas reports no `builtin` flags
  const supported = ps.some((p) => p.builtin !== undefined);

  return (
    <section>
      <h2 className="group-title">Personas</h2>
      <div className="inset">
        {list.data == null && list.error == null && <div className="row"><Loading /></div>}
        {ps.map((p) => (
          <PersonaRow key={p.name} p={p} disabled={!online || !supported} onNote={setNote} />
        ))}
        {adding ? (
          <PersonaForm
            title="New persona"
            submitLabel="Add"
            busyLabel="Adding…"
            onCancel={() => setAdding(false)}
            onSubmit={async (name, description) => {
              const p = await api.createPersona(name, description);
              setAdding(false);
              setNote(`Added ${p.name}. Pick it from the menu bar or the persona switcher.`);
              changed();
            }}
          />
        ) : (
          <div className="row">
            <button
              type="button"
              className="btn-plain btn"
              disabled={!online || !supported}
              onClick={() => {
                setNote(null);
                setAdding(true);
              }}
            >
              + Add persona
            </button>
          </div>
        )}
      </div>
      {list.error != null && <ErrorLine error={list.error} />}
      <p className="footnote" aria-live="polite">
        {note ??
          (supported || !online
            ? "The tone tells Claude how to write for each persona. Your own writing shapes it more as you add samples under it (Style → Voice)."
            : "Update the engine to add your own personas.")}
      </p>
    </section>
  );
}

function PersonaRow({ p, disabled, onNote }: { p: Persona; disabled: boolean; onNote: (s: string | null) => void }) {
  const [mode, setMode] = useState<"view" | "edit" | "delete">("view");
  const samples = p.samples ?? 0;
  const reset = useAction(async () => {
    await api.updatePersona(p.name, null);
    onNote(`${p.name} uses Polytone's tone again.`);
    changed();
  });
  const del = useAction(async () => {
    const r = await api.deletePersona(p.name);
    patchConfigLocally({ active_persona: r.active_persona });
    onNote(
      r.moved_samples
        ? `Deleted ${r.name}. Its ${samplesLabel(r.moved_samples)} now count toward Default.`
        : `Deleted ${r.name}.`,
    );
    changed();
  });

  if (mode === "edit") {
    return (
      <PersonaForm
        title={`${p.name} tone`}
        fixedName={p.name}
        // a name learned only from samples has a generic placeholder, not a tone worth editing
        initialDescription={p.builtin || p.edited ? p.description : ""}
        submitLabel="Save"
        busyLabel="Saving…"
        onCancel={() => setMode("view")}
        onSubmit={async (_name, description) => {
          if (description === p.description) {
            setMode("view"); // unchanged: don't mark a built-in as "your wording"
            return;
          }
          await api.updatePersona(p.name, description);
          setMode("view");
          onNote(`Saved ${p.name}'s tone.`);
          changed();
        }}
      />
    );
  }

  return (
    <>
      <div className="row">
        <div className="min-w-0 flex-1">
          <div className="text-[13px]">
            {p.name}
            <span className="ml-1.5 text-[11.5px] text-text-3">
              {p.builtin ? (p.edited ? "Built in · your wording" : "Built in") : "Yours"}
              {samples > 0 && ` · ${samplesLabel(samples)}`}
            </span>
          </div>
          {p.description && <div className="text-[11.5px] leading-snug text-text-3">{p.description}</div>}
        </div>
        {mode === "view" && (
          <div className="flex shrink-0 items-center gap-1.5">
            <button type="button" className="btn" disabled={disabled} onClick={() => setMode("edit")}>
              Edit
            </button>
            {p.builtin && p.edited && (
              <button type="button" className="btn" disabled={disabled || reset.busy} onClick={() => void reset.run()}>
                {reset.busy ? "Resetting…" : "Reset"}
              </button>
            )}
            {!p.builtin && (
              <button
                type="button"
                className="btn !text-bad"
                disabled={disabled}
                onClick={() => {
                  onNote(null);
                  setMode("delete");
                }}
              >
                Delete…
              </button>
            )}
          </div>
        )}
      </div>
      {mode === "delete" && (
        <div className="row fade-in flex-col !items-stretch !gap-2 !py-2.5">
          <p className="text-[12px] text-text-2">
            Delete {p.name}?{" "}
            {samples > 0
              ? `Its ${samplesLabel(samples)} move to Default, so nothing you wrote is lost.`
              : "It has no samples, so nothing else changes."}
          </p>
          <div className="flex items-center justify-end gap-2">
            <button type="button" className="btn" disabled={del.busy} onClick={() => setMode("view")}>
              Cancel
            </button>
            <button
              type="button"
              className="btn !bg-bad !text-on-accent"
              disabled={disabled || del.busy}
              autoFocus
              onClick={() => void del.run()}
            >
              {del.busy ? "Deleting…" : "Delete"}
            </button>
          </div>
          {del.error != null && <ErrorLine error={del.error} />}
        </div>
      )}
      {reset.error != null && (
        <div className="row">
          <ErrorLine error={reset.error} />
        </div>
      )}
    </>
  );
}

function PersonaForm({
  title,
  fixedName,
  initialDescription = "",
  submitLabel,
  busyLabel,
  onCancel,
  onSubmit,
}: {
  title: string;
  fixedName?: string;
  initialDescription?: string;
  submitLabel: string;
  busyLabel: string;
  onCancel: () => void;
  onSubmit: (name: string, description: string) => Promise<void>;
}) {
  const [name, setName] = useState(fixedName ?? "");
  const [description, setDescription] = useState(initialDescription);
  const save = useAction(onSubmit);
  const ready = name.trim() !== "" && description.trim() !== "";

  return (
    <form
      className="row fade-in flex-col !items-stretch !gap-2 !py-2.5"
      aria-label={title}
      onSubmit={(e) => {
        e.preventDefault();
        if (ready) void save.run(name.trim(), description.trim());
      }}
      onKeyDown={(e) => e.key === "Escape" && onCancel()}
    >
      <div className="text-[12px] font-medium text-text-2">{title}</div>
      {fixedName == null && (
        <input
          className="field text-[13px]"
          aria-label="Persona name"
          placeholder="Name, e.g. Recruiter"
          maxLength={NAME_MAX}
          value={name}
          autoFocus
          autoComplete="off"
          onChange={(e) => setName(e.target.value)}
        />
      )}
      <textarea
        className="field h-[64px] resize-none text-[12.5px] leading-[1.45]"
        aria-label="Tone"
        placeholder="Tone, e.g. warm and brief, always ends with a clear next step"
        maxLength={DESCRIPTION_MAX}
        value={description}
        autoFocus={fixedName != null}
        onChange={(e) => setDescription(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && ready) {
            e.preventDefault();
            void save.run(name.trim(), description.trim());
          }
        }}
      />
      <div className="flex items-center justify-end gap-2">
        <button type="button" className="btn" onClick={onCancel} disabled={save.busy}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary" disabled={!ready || save.busy}>
          {save.busy ? busyLabel : submitLabel}
        </button>
      </div>
      {save.error != null && <ErrorLine error={save.error} />}
    </form>
  );
}
