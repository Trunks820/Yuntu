import {
  getNoRouteExplanation,
  getOutcomeDisplay,
  getQuotaNotice,
} from "@/constants/delivery";
import { webErrorMessage } from "@/constants/errors";
import { STAGE_MAP, TOTAL_STAGES } from "@/constants/stages";
import type {
  TaskNotificationState,
  TripTaskStatus,
} from "@/stores/tripTaskStore";
import {
  MAX_POLLING_DURATION_MS,
  useTripTaskStore,
} from "@/stores/tripTaskStore";
import type { Destination } from "@/types/destination";
import type {
  JobResponse,
  PublicGenerationDelivery,
  StageCode,
  StageProgress,
  TripResult,
} from "@/types/trip";
import type { TripSubmitRequest } from "@/types/tripRequest";
import { cleanTripCityName, hasTripStageProgress } from "@/utils/tripShelf";

/** 四级镜头缩放：全国 → 大区 → 城市 → 主城（与 PC 原型 BZ 一致） */
export const BIND_ZOOM_LEVELS = [4.75, 7, 9.75, 11.5] as const;

/** 天色长卷四段中心横坐标（%），与 PC 原型 BX 一致 */
export const BIND_STEP_POSITIONS = [12.5, 37.5, 62.5, 87.5] as const;

/** 无城市坐标或坐标缺失时，地图停在全国一级，以中国中部为中心（不写死任何城市坐标） */
export const CHINA_FALLBACK_CENTER = {
  longitude: 104.5,
  latitude: 35.5,
} as const;

/** 推近时让开左边卡片与底部长卷的安全留白（与原型 BHUD = 340, BLOW = 250 一致） */
export const BIND_VIEWPORT_PADDING = {
  hudLeft: 340,
  topMargin: 16,
  stripBottom: 250,
} as const;

export interface SkyStageMeta {
  step: number;
  code: StageCode;
  period: string;
  label: string;
  positionPct: number;
}

export const SKY_STAGES: readonly SkyStageMeta[] = [
  {
    step: STAGE_MAP.ANALYZING.step,
    code: "ANALYZING",
    period: "上午",
    label: STAGE_MAP.ANALYZING.label,
    positionPct: BIND_STEP_POSITIONS[0],
  },
  {
    step: STAGE_MAP.PLANNING.step,
    code: "PLANNING",
    period: "下午",
    label: STAGE_MAP.PLANNING.label,
    positionPct: BIND_STEP_POSITIONS[1],
  },
  {
    step: STAGE_MAP.COMPOSING.step,
    code: "COMPOSING",
    period: "傍晚",
    label: STAGE_MAP.COMPOSING.label,
    positionPct: BIND_STEP_POSITIONS[2],
  },
  {
    step: STAGE_MAP.FINALIZING.step,
    code: "FINALIZING",
    period: "夜间",
    label: STAGE_MAP.FINALIZING.label,
    positionPct: BIND_STEP_POSITIONS[3],
  },
] as const;

export type SpreadPlanningVisualState =
  | "binding"
  | "no_route"
  | "failed"
  | "timeout"
  | "unknown"
  | "completed";

export interface PlanningCoordinates {
  longitude: number;
  latitude: number;
}

export interface PlanningConditionSummary {
  leadLine: string | null;
  rows: Array<{ label: string; value: string }>;
}

const GENERIC_PLACEHOLDER_DESTINATIONS = new Set([
  "目的地",
  "当前目的地",
  "行程",
]);

const CN_DAYS = "一两三四五六七";
const MAX_MERCATOR_LAT = 85.05112878;

const COMMUTE_LABELS: Record<string, string> = {
  driving: "打车 / 自驾",
  transit: "公共交通",
  cycling: "骑行",
  walking: "步行",
};

/** 仅认可有限且非占位的 stage_progress 步数，否则返回 null（不默认第 1 步） */
export function normalizePlanningStep(
  stageProgress: StageProgress | null | undefined,
): number | null {
  if (!hasTripStageProgress(stageProgress)) {
    return null;
  }
  const rounded = Math.round(stageProgress.step);
  if (!Number.isFinite(rounded) || rounded < 1) {
    return null;
  }
  return Math.min(TOTAL_STAGES, rounded);
}

/** 步数（1..4）+ 是否有坐标 → 镜头级别（4.75 / 7 / 9.75 / 11.5） */
export function resolvePlanningZoomLevel(
  knownStep: number | null,
  hasCoordinates: boolean,
): number {
  if (!hasCoordinates || knownStep === null) {
    return BIND_ZOOM_LEVELS[0];
  }
  const idx = Math.min(Math.max(knownStep, 1), BIND_ZOOM_LEVELS.length) - 1;
  return BIND_ZOOM_LEVELS[idx];
}

/** 计算天色长卷亮起进度（%）：无步数时为 0%，完成后亮满 100% */
export function resolveStripProgressPct(
  knownStep: number | null,
  visualState: SpreadPlanningVisualState,
): string {
  if (visualState === "completed") {
    return "100%";
  }
  if (knownStep === null) {
    return "0%";
  }
  const idx = Math.min(Math.max(knownStep, 1), BIND_STEP_POSITIONS.length) - 1;
  return `${BIND_STEP_POSITIONS[idx] + 4}%`;
}

/**
 * 计算左侧卡片与底部长卷之间空地的中心屏幕坐标，用于把城市点落在正中。
 */
export function computeBindOpenCenterPx(
  viewportWidth: number,
  viewportHeight: number,
): { fx: number; fy: number } {
  const hudRight = Math.min(BIND_VIEWPORT_PADDING.hudLeft, viewportWidth * 0.4);
  const stripTop = Math.max(
    BIND_VIEWPORT_PADDING.topMargin + 40,
    viewportHeight - BIND_VIEWPORT_PADDING.stripBottom,
  );
  return {
    fx: (hudRight + viewportWidth) / 2,
    fy: (BIND_VIEWPORT_PADDING.topMargin + stripTop) / 2,
  };
}

/**
 * Web Mercator (EPSG:3857) 偏移中心点算术：让目标经纬度在给定 zoom 下落入 (fx, fy)。
 * 当宽高无效（如无尺寸测试环境）时直接返回原始经纬度。
 */
export function computeBindOffsetCenter(
  target: PlanningCoordinates,
  zoom: number,
  viewportWidth: number,
  viewportHeight: number,
): [number, number] {
  if (
    !Number.isFinite(viewportWidth) ||
    !Number.isFinite(viewportHeight) ||
    viewportWidth <= 0 ||
    viewportHeight <= 0
  ) {
    return [target.longitude, target.latitude];
  }

  const clampedLat = Math.max(
    -MAX_MERCATOR_LAT,
    Math.min(MAX_MERCATOR_LAT, target.latitude),
  );
  const worldSize = 256 * Math.pow(2, zoom);
  const sinLat = Math.sin((clampedLat * Math.PI) / 180);
  const targetX = ((target.longitude + 180) / 360) * worldSize;
  const targetY =
    (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * worldSize;

  const { fx, fy } = computeBindOpenCenterPx(viewportWidth, viewportHeight);
  const centerX = targetX - (fx - viewportWidth / 2);
  const centerY = targetY - (fy - viewportHeight / 2);

  const centerLng = (centerX / worldSize) * 360 - 180;
  const n = Math.PI - (2 * Math.PI * centerY) / worldSize;
  const centerLat =
    (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));

  if (!Number.isFinite(centerLng) || !Number.isFinite(centerLat)) {
    return [target.longitude, target.latitude];
  }
  return [centerLng, centerLat];
}

/**
 * 按产品规则解析城市名：
 * 1. 先看 tripTaskStore 里这个 job 的 destination（跳过空串与“目的地”占位符）
 * 2. 否则仅当 tripStore.currentJobId === jobId 时，取 formData.to_city
 * 3. 都没有返回 null，不拿别的城市顶替
 */
export function resolvePlanningCityName(options: {
  taskDestination?: string | null;
  tripCurrentJobId?: string | null;
  jobId: string;
  formCity?: string | null;
}): string | null {
  const fromTask = options.taskDestination?.trim();
  if (fromTask && !GENERIC_PLACEHOLDER_DESTINATIONS.has(fromTask)) {
    const cleaned = cleanTripCityName(fromTask);
    if (cleaned && !GENERIC_PLACEHOLDER_DESTINATIONS.has(cleaned)) {
      return cleaned;
    }
  }

  if (options.tripCurrentJobId === options.jobId) {
    const fromForm = options.formCity?.trim();
    if (fromForm && !GENERIC_PLACEHOLDER_DESTINATIONS.has(fromForm)) {
      const cleaned = cleanTripCityName(fromForm);
      if (cleaned && !GENERIC_PLACEHOLDER_DESTINATIONS.has(cleaned)) {
        return cleaned;
      }
    }
  }

  return null;
}

/**
 * 只从 /api/destinations 返回的同名城市里取坐标；拿不到或请求失败返回 null。
 */
export function resolvePlanningCoordinates(
  cityName: string | null,
  destinations: readonly Destination[] | undefined,
): PlanningCoordinates | null {
  if (!cityName || !destinations || destinations.length === 0) {
    return null;
  }
  const targetClean = cleanTripCityName(cityName);
  if (!targetClean) {
    return null;
  }

  const matched = destinations.find(
    (dest) => cleanTripCityName(dest.name) === targetClean,
  );
  const lng = matched?.coordinates?.longitude;
  const lat = matched?.coordinates?.latitude;

  if (
    typeof lng === "number" &&
    typeof lat === "number" &&
    Number.isFinite(lng) &&
    Number.isFinite(lat) &&
    Math.abs(lng) <= 180 &&
    Math.abs(lat) <= 90 &&
    (lng !== 0 || lat !== 0)
  ) {
    return { longitude: lng, latitude: lat };
  }

  return null;
}

/** 天数、必去地点等条件只在 currentJobId === jobId 时从 tripStore.formData 读取 */
export function resolveMatchedFormData(
  formData: TripSubmitRequest | null,
  currentJobId: string | null,
  jobId: string,
): TripSubmitRequest | null {
  if (!formData || !jobId || currentJobId !== jobId) {
    return null;
  }
  return formData;
}

export function formatCnDayCount(
  days: number | null | undefined,
): string | null {
  if (typeof days !== "number" || !Number.isInteger(days) || days < 1) {
    return null;
  }
  if (days <= CN_DAYS.length) {
    return CN_DAYS[days - 1];
  }
  return String(days);
}

export function formatElapsedClock(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds));
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function computeTimeoutMinutes(
  durationMs: number = MAX_POLLING_DURATION_MS,
): number {
  return Math.max(1, Math.round(durationMs / 60_000));
}

export function buildBindingHeadline(
  cityName: string | null,
  days: number | null | undefined,
): { lines: string[]; fullText: string } {
  const cnDays = formatCnDayCount(days);
  if (cityName && cnDays) {
    const lines = [`正在把${cityName}，`, `排成你的${cnDays}天。`];
    return { lines, fullText: lines.join("") };
  }
  if (cityName) {
    const line = `正在把${cityName}排成你的行程。`;
    return { lines: [line], fullText: line };
  }
  const line = "正在排你的行程。";
  return { lines: [line], fullText: line };
}

function parseCompanionFromNotes(rawNotes: string | undefined): {
  companionSummary: string | null;
  userNotes: string | null;
} {
  if (!rawNotes || !rawNotes.trim()) {
    return { companionSummary: null, userNotes: null };
  }
  const lines = rawNotes
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean);
  let companionSummary: string | null = null;
  const userLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith("【同行情况】")) {
      companionSummary = line
        .replace(/^【同行情况】/, "")
        .replace(/。$/, "")
        .trim();
    } else {
      userLines.push(line);
    }
  }

  return {
    companionSummary,
    userNotes: userLines.length > 0 ? userLines.join(" ") : null,
  };
}

/** 只从拿得到的请求条件整理左卡片下方展示内容 */
export function extractPlanningConditionSummary(
  formData: TripSubmitRequest | null,
): PlanningConditionSummary {
  if (!formData) {
    return { leadLine: null, rows: [] };
  }

  const { companionSummary, userNotes } = parseCompanionFromNotes(
    formData.notes,
  );
  const cleanCity = cleanTripCityName(formData.to_city || "");
  const cnDays = formatCnDayCount(formData.days);

  const leadParts: string[] = [];
  if (cleanCity && cnDays) {
    leadParts.push(`想去${cleanCity}，待${cnDays}天`);
  } else if (cleanCity) {
    leadParts.push(`想去${cleanCity}`);
  } else if (cnDays) {
    leadParts.push(`行程${cnDays}天`);
  }

  if (companionSummary) {
    leadParts.push(companionSummary);
  } else if (
    typeof formData.people_count === "number" &&
    Number.isInteger(formData.people_count) &&
    formData.people_count > 0
  ) {
    leadParts.push(`${formData.people_count} 人`);
  }

  if (formData.preferences && formData.preferences.length > 0) {
    leadParts.push(formData.preferences.join(" · "));
  }

  const rows: Array<{ label: string; value: string }> = [];

  if (formData.start_date && formData.end_date) {
    rows.push({
      label: "日期",
      value: `${formData.start_date} 至 ${formData.end_date}`,
    });
  } else if (formData.start_date) {
    rows.push({ label: "日期", value: `${formData.start_date} 出发` });
  }

  if (formData.commute_mode && COMMUTE_LABELS[formData.commute_mode]) {
    rows.push({
      label: "城内出行",
      value: COMMUTE_LABELS[formData.commute_mode],
    });
  }

  if (formData.avoid && formData.avoid.length > 0) {
    rows.push({ label: "避开", value: formData.avoid.join("、") });
  }

  if (formData.must_include && formData.must_include.length > 0) {
    rows.push({
      label: "还想去",
      value: formData.must_include.map((m) => m.name).join("、"),
    });
  }

  if (formData.accommodation?.name?.trim()) {
    rows.push({ label: "住在", value: formData.accommodation.name.trim() });
  }

  if (formData.from_city?.trim()) {
    rows.push({ label: "出发地", value: formData.from_city.trim() });
  }

  if (userNotes) {
    rows.push({ label: "备注", value: userNotes });
  }

  return {
    leadLine: leadParts.length > 0 ? `${leadParts.join("，")}。` : null,
    rows,
  };
}

export function buildCompletedRedirectUrl(
  recordId: string,
  jobId: string,
  result: TripResult | null,
): string {
  if (result && result.plans.length === 1) {
    return `/plan/${recordId}/${result.plans[0].plan_id}?job_id=${jobId}`;
  }
  return `/result/${recordId}?job_id=${jobId}`;
}

export interface SpreadPlanningCopy {
  hudLines: string[];
  hudFullTitle: string;
  hudNote: string | null;
  stripTitle: string | null;
  stripSub: string | null;
  stripFailStyle: boolean;
  srAnnouncement: string;
}

export function buildPlanningStateCopy(params: {
  visualState: SpreadPlanningVisualState;
  cityName: string | null;
  days: number | null | undefined;
  knownStep: number | null;
  delivery: PublicGenerationDelivery | null;
  errorCode: string | null;
  errorMessage: string | null;
  timeoutMinutes?: number;
}): SpreadPlanningCopy {
  const {
    visualState,
    cityName,
    days,
    knownStep,
    delivery,
    errorCode,
    errorMessage,
    timeoutMinutes = computeTimeoutMinutes(MAX_POLLING_DURATION_MS),
  } = params;

  const quotaNotice = delivery?.quota_state
    ? getQuotaNotice(delivery.quota_state)
    : null;
  const stepLabel =
    knownStep !== null ? (SKY_STAGES[knownStep - 1]?.label ?? null) : null;

  switch (visualState) {
    case "no_route": {
      const reason = getNoRouteExplanation(
        delivery?.reason_code ?? null,
      ).replace(/。$/, "");
      const hudNote = `${reason}。条件都还在，可以改一改再试。`;
      const stripTitle =
        knownStep !== null
          ? `停在第 ${knownStep} 步`
          : getOutcomeDisplay("NO_ROUTE").label;
      return {
        hudLines: ["这次没能排出路线"],
        hudFullTitle: "这次没能排出路线",
        hudNote,
        stripTitle,
        stripSub: quotaNotice,
        stripFailStyle: true,
        srAnnouncement: quotaNotice ? `${hudNote} ${quotaNotice}` : hudNote,
      };
    }

    case "failed": {
      const reason = webErrorMessage(errorCode, errorMessage);
      return {
        hudLines: ["这一册没能装订"],
        hudFullTitle: "这一册没能装订",
        hudNote: reason,
        stripTitle: getOutcomeDisplay("FAILED").label,
        stripSub: quotaNotice,
        stripFailStyle: true,
        srAnnouncement: quotaNotice ? `${reason} ${quotaNotice}` : reason,
      };
    }

    case "timeout": {
      const hudNote = "它还在后台继续装订，好了会出现在‘我的行程’里。";
      const stripTitle =
        knownStep !== null ? `第 ${knownStep} / ${TOTAL_STAGES} 步` : null;
      const stripSub = `已等 ${timeoutMinutes} 分钟，页面不再自动刷新`;
      return {
        hudLines: ["这次比平时慢"],
        hudFullTitle: "这次比平时慢",
        hudNote,
        stripTitle,
        stripSub,
        stripFailStyle: false,
        srAnnouncement: stripTitle
          ? `等待超时，${stripTitle}，${stripSub}`
          : `等待超时，${stripSub}`,
      };
    }

    case "unknown": {
      const headline = buildBindingHeadline(cityName, days);
      const stripTitle =
        knownStep !== null
          ? `第 ${knownStep} / ${TOTAL_STAGES} 步`
          : "状态待确认";
      return {
        hudLines: headline.lines,
        hudFullTitle: headline.fullText,
        hudNote: "暂时无法确认任务状态，任务可能仍在继续。",
        stripTitle,
        stripSub: "已暂停自动刷新",
        stripFailStyle: false,
        srAnnouncement: "任务状态暂未确认，可能仍在继续。",
      };
    }

    case "completed": {
      const headline = buildBindingHeadline(cityName, days);
      const isBasic = delivery?.outcome === "BASIC";
      const stripTitle = isBasic ? "装订好了 · 基础版" : "装订好了";
      return {
        hudLines: headline.lines,
        hudFullTitle: headline.fullText,
        hudNote: isBasic
          ? "已为你整理基础行程，即将翻开旅行册。"
          : "正在为你翻开旅行册。",
        stripTitle,
        stripSub: quotaNotice,
        stripFailStyle: false,
        srAnnouncement: quotaNotice
          ? `${stripTitle} · ${quotaNotice}`
          : stripTitle,
      };
    }

    case "binding":
    default: {
      const headline = buildBindingHeadline(cityName, days);
      const stripTitle =
        knownStep !== null ? `第 ${knownStep} / ${TOTAL_STAGES} 步` : null;
      return {
        hudLines: headline.lines,
        hudFullTitle: headline.fullText,
        hudNote: null,
        stripTitle,
        stripSub: null,
        stripFailStyle: false,
        srAnnouncement:
          knownStep !== null && stepLabel
            ? `第 ${knownStep} 步，共 ${TOTAL_STAGES} 步：${stepLabel}`
            : "正在准备生成行程",
      };
    }
  }
}

/**
 * 集中写入 tripTaskStore 任务记录，便于后续统一接管任务状态写入。
 */
export function syncPlanningTaskRecord(params: {
  jobId: string;
  data?: JobResponse;
  destination?: string | null;
  startedAt?: number;
  requestId?: string | null;
  status?: TripTaskStatus;
  notificationState?: TaskNotificationState;
  terminalStatus?: "ready" | "no_route" | "failed";
  error?: { code: string; message: string } | null;
}): void {
  const {
    jobId,
    data,
    destination,
    startedAt,
    requestId,
    status,
    notificationState,
    terminalStatus,
    error,
  } = params;
  const existing = useTripTaskStore.getState().getTask(jobId);

  const patch: Parameters<
    ReturnType<typeof useTripTaskStore.getState>["addOrUpdateTask"]
  >[0] = {
    jobId,
  };

  if (
    destination &&
    !GENERIC_PLACEHOLDER_DESTINATIONS.has(destination.trim())
  ) {
    patch.destination = destination;
  } else if (
    existing?.destination &&
    !GENERIC_PLACEHOLDER_DESTINATIONS.has(existing.destination)
  ) {
    patch.destination = existing.destination;
  }

  if (requestId !== undefined) {
    patch.requestId = requestId;
  }

  if (!existing && startedAt !== undefined) {
    patch.startedAt = startedAt;
  }

  if (data?.delivery) {
    patch.delivery = data.delivery;
  }
  if (data?.generation_policy_version != null) {
    patch.generationPolicyVersion = data.generation_policy_version;
  }
  if (data?.result_record_id != null) {
    patch.resultRecordId = data.result_record_id;
  }

  if (terminalStatus) {
    patch.status = terminalStatus;
    patch.finishedAt = existing?.finishedAt ?? Date.now();
    if (notificationState !== undefined) {
      patch.notificationState = notificationState;
    }
  } else if (status !== undefined) {
    patch.status = status;
    if (notificationState !== undefined) {
      patch.notificationState = notificationState;
    }
  } else if (notificationState !== undefined) {
    patch.notificationState = notificationState;
  }

  if (error !== undefined) {
    patch.error = error;
  }

  useTripTaskStore.getState().addOrUpdateTask(patch);
}
