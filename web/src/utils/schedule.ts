import type {
  ExactTimeSource,
  PlaceScheduleInfo,
  SchedulePeriod,
  TripTimePreferences,
} from "@/types/trip";

/**
 * v0.8.12：前端不再合成任何休闲时间线（旧 computeSchedule 的 09:00 累加已删除）。
 * 时段与精确时间一律来自后端 place.schedule，前端只做展示映射与来源校验。
 */

const PERIOD_LABEL: Record<SchedulePeriod, string> = {
  morning: "上午",
  afternoon: "下午",
  evening: "傍晚",
  night: "晚上",
};

const EXACT_TIME_SOURCES: ReadonlySet<string> = new Set<ExactTimeSource>([
  "reservation",
  "event",
  "transport",
  "verified_venue_rule",
  "planned",
]);

const HHMM_RE = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

/** 时段中文标签；未知/缺失（Schema 1.4）返回空串，调用方按无时段处理 */
export function periodLabel(period: string | null | undefined): string {
  if (!period) return "";
  return PERIOD_LABEL[period as SchedulePeriod] ?? "";
}

export interface GovernedExactTime {
  /** "14:30 - 16:00" / "14:30 起" / "16:00 前" / "约 09:45" 之类的展示文本 */
  text: string;
  source: ExactTimeSource;
}

/**
 * 仅当 exact_time_source 为受治理来源且 exact_* 是合法 HH:MM 时返回展示文本，
 * 其余情况一律返回 null（疑似时间文本不得显示成确定时间）。
 * - planned：按行程推算的参考时间，只看 exact_start，展示"约 HH:MM"（不加"起"，不显示区间）。
 */
export function governedExactTime(
  schedule: PlaceScheduleInfo | null | undefined,
): GovernedExactTime | null {
  if (!schedule) return null;
  const source = schedule.exact_time_source;
  if (!source || !EXACT_TIME_SOURCES.has(source)) return null;

  const start = schedule.exact_start && HHMM_RE.test(schedule.exact_start) ? schedule.exact_start : null;
  if (source === "planned") {
    if (!start) return null;
    return { text: `约 ${start}`, source: "planned" };
  }

  const end = schedule.exact_end && HHMM_RE.test(schedule.exact_end) ? schedule.exact_end : null;
  if (!start && !end) return null;

  let text: string;
  if (start && end) text = `${start} - ${end}`;
  else if (start) text = `${start} 起`;
  else text = `${end} 前`;
  return { text, source };
}

/** 精确时间来源中文说明（展示在时间旁的小标注） */
export function exactTimeSourceLabel(source: ExactTimeSource): string {
  switch (source) {
    case "reservation":
      return "预约";
    case "event":
      return "活动";
    case "transport":
      return "交通";
    case "verified_venue_rule":
      return "场馆规则";
    case "planned":
      return "参考";
  }
}

/** Schema 版本 ≥ 1.5 判定。非法/缺失版本号按旧版本处理 */
export function isSchema15(version: string | null | undefined): boolean {
  if (!version) return false;
  const [maj, min] = String(version).split(".").map(Number);
  if (!Number.isFinite(maj)) return false;
  return maj > 1 || (maj === 1 && (min ?? 0) >= 5);
}

/**
 * 结果页时间偏好文案。
 * - 1.5 起后端保证无 time_preferences 即"无固定时间"，可如实展示；
 * - 1.4 旧结果没有该字段，返回 null（不展示，不伪造断言）。
 */
export function timePreferencesLabel(
  schemaVersion: string | null | undefined,
  prefs: TripTimePreferences | null | undefined,
): string | null {
  const start = prefs?.daily_start || null;
  const end = prefs?.daily_end || null;
  if (start && end) return `每天 ${start} - ${end}`;
  if (start) return `每天 ${start} 后出发`;
  if (end) return `每天 ${end} 前结束`;
  return isSchema15(schemaVersion) ? "无固定时间" : null;
}
