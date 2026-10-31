/**
 * Mechanical odometer counter. Each digit is a drum of 0-9 that rolls to its
 * value; the tenths drum is printed in ribbon red like a real trip meter.
 */
export function Odometer({ value, intDigits = 3 }: { value: number | null; intDigits?: number }) {
  const blank = value == null || Number.isNaN(value);
  const clamped = blank ? 0 : Math.max(0, Math.min(10 ** intDigits - 0.1, value));
  const fixed = clamped.toFixed(1); // e.g. "87.4"
  const [intPart, frac] = fixed.split(".");
  const ints = intPart.padStart(intDigits, "0").split("");

  return (
    <div
      className="inline-flex items-stretch gap-[3px] rounded-[6px] p-[5px]"
      role="img"
      aria-label={blank ? "No score yet" : `Style distance score ${fixed} out of 100`}
      style={{
        background: "linear-gradient(180deg, #2b2a26, #121110)",
        boxShadow:
          "inset 0 1px 0 rgb(255 255 255 / 0.08), 0 2px 0 #000, 0 6px 14px -6px rgb(0 0 0 / 0.5)",
      }}
    >
      {ints.map((d, i) => (
        <Drum key={`i${i}`} digit={blank ? null : Number(d)} dim={!blank && i < intDigits - intPart.length} />
      ))}
      <div className="flex items-end pb-[6px]">
        <span className="block h-[4px] w-[4px] rounded-full bg-[#e9e3d2]" />
      </div>
      <Drum digit={blank ? null : Number(frac)} red />
    </div>
  );
}

function Drum({ digit, red = false, dim = false }: { digit: number | null; red?: boolean; dim?: boolean }) {
  const h = 46;
  return (
    <div
      className="relative overflow-hidden rounded-[3px]"
      style={{
        width: 32,
        height: h,
        background: red
          ? "linear-gradient(180deg, #7a140e, #b3261e 30%, #c7372c 50%, #b3261e 70%, #6e120c)"
          : "linear-gradient(180deg, #b8b2a2, #f4efe2 28%, #fffcf3 50%, #f4efe2 72%, #a9a393)",
        boxShadow: "inset 0 0 0 1px rgb(0 0 0 / 0.35)",
      }}
    >
      <div
        className="absolute left-0 right-0 top-0 transition-transform duration-700 ease-[cubic-bezier(.2,.8,.2,1)]"
        style={{ transform: `translateY(${-(digit ?? 0) * h}px)` }}
      >
        {Array.from({ length: 10 }, (_, n) => (
          <div
            key={n}
            className="flex items-center justify-center font-mono font-semibold tabular-nums"
            style={{
              height: h,
              fontSize: 30,
              color: red ? "#fff3ea" : dim ? "#a59f8f" : "#1a1916",
              opacity: digit == null ? 0 : 1,
            }}
          >
            {n}
          </div>
        ))}
      </div>
      {digit == null && (
        <div
          className="absolute inset-0 flex items-center justify-center font-mono text-[22px]"
          style={{ color: red ? "#fff3ea" : "#8a8474" }}
        >
          –
        </div>
      )}
      {/* glass sheen + drum curvature */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "linear-gradient(180deg, rgb(0 0 0 / 0.35), transparent 22%, transparent 78%, rgb(0 0 0 / 0.35))",
        }}
      />
    </div>
  );
}
