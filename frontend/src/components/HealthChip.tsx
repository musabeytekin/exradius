import type { HealthInfo } from "@/lib/projectConfig";
import { Tooltip } from "./ui/overlays";
import { Chip } from "./ui/primitives";

export function HealthChip({ info }: { info: HealthInfo }) {
  const chip = <Chip tone={info.tone} dot>{info.label}</Chip>;
  return info.detail ? <Tooltip content={info.detail}><span className="inline-flex">{chip}</span></Tooltip> : chip;
}
