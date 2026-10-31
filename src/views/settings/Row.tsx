import type { ReactNode } from "react";

/** One line in an inset settings group: title, optional subtitle, trailing control. */
export function Row({ title, subtitle, children }: { title: ReactNode; subtitle?: ReactNode; children?: ReactNode }) {
  return (
    <div className="row">
      <div className="min-w-0 flex-1">
        <div className="text-[13px]">{title}</div>
        {subtitle && <div className="text-[11.5px] leading-snug text-text-3">{subtitle}</div>}
      </div>
      {children}
    </div>
  );
}
