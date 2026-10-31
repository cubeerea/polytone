import type { Health } from "../api";
import { isOffline } from "../api";
import { inTauri, type RunResult } from "../tauri";
import { useConfig, usePermissions } from "../queries";
import { Disclosure, ErrorLine } from "../components/bits";
import { PermissionsCard } from "./settings/PermissionsCard";
import { ShortcutGroup } from "./settings/ShortcutGroup";
import { CaptureGroup } from "./settings/CaptureGroup";
import { AdvancedGroup } from "./settings/AdvancedGroup";

interface Props {
  online: boolean;
  health: Health | null;
  lastRunFromEvent: RunResult | null;
  generating: boolean;
}

export function Settings({ online, health, lastRunFromEvent, generating }: Props) {
  const tauri = inTauri();
  const config = useConfig();
  const perm = usePermissions();
  // Offline is already explained by the header and the disabled controls.
  const configError = config.error != null && !isOffline(config.error) ? config.error : null;

  return (
    <div className="space-y-7 pt-1">
      {tauri ? (
        perm.data && !perm.data.accessibility && <PermissionsCard perm={perm.data} onRecheck={perm.reload} />
      ) : (
        <p className="rounded-lg bg-fill px-3 py-2 text-[12px] text-text-2">
          Running in a browser. The shortcut, permissions and typing capture only work inside the Polytone app.
        </p>
      )}

      <ShortcutGroup tauri={tauri} online={online} config={config.data} />
      {configError != null && <ErrorLine error={configError} />}

      <CaptureGroup tauri={tauri} online={online} config={config.data} />

      <Disclosure title="Advanced">
        <AdvancedGroup
          tauri={tauri}
          online={online}
          health={health}
          perm={perm.data}
          permError={perm.error}
          lastRunFromEvent={lastRunFromEvent}
          generating={generating}
        />
      </Disclosure>
    </div>
  );
}
