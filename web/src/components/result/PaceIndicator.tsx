import type { PaceLevel, CommuteStatus } from "@/types/trip";
import { formatMinutes } from "@/utils/format";

interface PaceIndicatorProps {
  level: PaceLevel;
  commuteStatus: CommuteStatus;
  totalMinutes: number;
}

const LEVEL_LABEL: Record<PaceLevel, string> = {
  RELAXED: "轻松",
  MODERATE: "适中",
  INTENSIVE: "紧凑",
};

const LEVEL_STYLE: Record<PaceLevel, string> = {
  RELAXED: "bg-primary-50 text-primary-700",
  MODERATE: "bg-accent-50 text-accent-700",
  INTENSIVE: "bg-accent-100 text-accent-800",
};

export function PaceIndicator({
  level,
  commuteStatus,
  totalMinutes,
}: PaceIndicatorProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className={`rounded-full px-2.5 py-1 font-medium ${LEVEL_STYLE[level]}`}>
        {LEVEL_LABEL[level]}
      </span>
      <span className="text-sand-500">
        总通勤 {formatMinutes(totalMinutes)}
      </span>
      {commuteStatus === "OVER_LIMIT" && (
        <span className="rounded-full bg-amber-50 px-2 py-0.5 text-amber-600">
          通勤偏长
        </span>
      )}
    </div>
  );
}
