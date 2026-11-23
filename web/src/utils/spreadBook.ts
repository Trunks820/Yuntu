import type {
  AccommodationInfo,
  CommuteLeg,
  LodgingDisplayGroup,
  LodgingDisplayNotice,
  PlaceDetail,
  PlaceGalleryImage,
  SchedulePeriod,
  TransitStep,
  TripDay,
  TripMustInclude,
  TripPlace,
  TripTransport,
  TripWeather,
  WeatherDay,
} from "@/types/trip";
import type {
  CostCategoryKey,
  CostPriceBasis,
  CostScenarioSummary,
  ScenarioCostStatus,
} from "@/types/cost";
import type { LucideIcon } from "lucide-react";
import { collectWeatherReminders, weatherFaIcon } from "@/constants/weather";
import {
  cleanBrief,
  cleanSummary,
  cleanTags,
  commuteModeName,
} from "@/utils/format";
import { governedExactTime, periodLabel } from "@/utils/schedule";

const CN_DAY_DIGITS = [
  "",
  "一",
  "二",
  "三",
  "四",
  "五",
  "六",
  "七",
  "八",
  "九",
  "十",
] as const;

const VALID_PERIODS = new Set<string>([
  "morning",
  "afternoon",
  "evening",
  "night",
]);

/** 第几天：一到十用汉字，超过十写“第 11 天”。 */
export function formatBookDayLabel(day: number): string {
  if (!Number.isInteger(day) || day < 1) {
    return "第 1 天";
  }
  if (day <= 10) {
    return `第${CN_DAY_DIGITS[day]}天`;
  }
  return `第 ${day} 天`;
}

/** 仅在 weather.status === "ok" 且存在对应 day 条目时返回当天天气，不猜测。 */
export function getDayWeather(
  weather: TripWeather | null | undefined,
  day: number,
): WeatherDay | null {
  if (!weather || weather.status !== "ok" || !Array.isArray(weather.days)) {
    return null;
  }
  return weather.days.find((w) => w.day === day) ?? null;
}

export interface DayWeatherBadge {
  Icon: LucideIcon;
  weatherText: string;
  tempRange: string;
}

/** 页签上的天气：线条图标（原型 wxChip 的样子），不用 emoji。 */
export function formatDayWeatherBadge(
  weatherDay: WeatherDay | null | undefined,
): DayWeatherBadge | null {
  if (!weatherDay) return null;
  return {
    Icon: weatherFaIcon(weatherDay.icon_code),
    weatherText: weatherDay.weather_text,
    tempRange: `${weatherDay.temp_min_c}–${weatherDay.temp_max_c}°`,
  };
}

/**
 * 建议停留时长：只在 stay_minutes > 0 时返回。
 * < 60 分钟写“建议 x 分钟”；>= 60 分钟换算小时（如“建议 1.5 小时”），整数小时去零。
 */
export function formatStayMinutes(
  minutes: number | null | undefined,
): string | null {
  if (
    typeof minutes !== "number" ||
    !Number.isFinite(minutes) ||
    minutes <= 0
  ) {
    return null;
  }
  const rounded = Math.round(minutes);
  if (rounded < 60) {
    return `建议 ${rounded} 分钟`;
  }
  const hours = Number((rounded / 60).toFixed(1));
  return `建议 ${hours} 小时`;
}

export interface StopScheduleMeta {
  period: SchedulePeriod | null;
  periodText: string | null;
  exactTimeText: string | null;
  combinedText: string | null;
}

/**
 * 仅从 place.schedule 读取时段；仅当 governedExactTime 认可时才附带准点时间。
 * 缺失 schedule 时全部返回 null，不补齐、不推测。
 */
export function resolveStopScheduleMeta(place: TripPlace): StopScheduleMeta {
  const schedule = place.schedule;
  if (!schedule) {
    return {
      period: null,
      periodText: null,
      exactTimeText: null,
      combinedText: null,
    };
  }
  const label = periodLabel(schedule.period) || null;
  const period =
    label && VALID_PERIODS.has(schedule.period)
      ? (schedule.period as SchedulePeriod)
      : null;
  const governed = governedExactTime(schedule);
  const exactTimeText = governed?.text ?? null;
  const combinedText = [label, exactTimeText].filter(Boolean).join(" ") || null;

  return {
    period,
    periodText: label,
    exactTimeText,
    combinedText,
  };
}

/**
 * 卡片说明：只放 activity_note。brief 是 travel 取的帖子推荐理由第一条，
 * 会是整条“建议路线：……”这类摘句，只留在右边详情里。
 */
export function resolveStopCardNote(place: TripPlace): string {
  return place.activity_note?.trim() ?? "";
}

export type StopKind =
  | "park"
  | "food"
  | "museum"
  | "sight"
  | "street"
  | "market"
  | "photo"
  | "other";

// travel place_type 只有 attraction/restaurant/business_area/market/park/museum/photo_spot/hotel/other；
// 吃喝的细分码来自 travel 的 FOOD/SNACK/COFFEE 分组，scenic/landmark/culture 是旧契约留下的取值。
const STOP_KIND_BY_CATEGORY: Record<string, StopKind> = {
  park: "park",
  restaurant: "food",
  food: "food",
  cafe: "food",
  coffee: "food",
  tea: "food",
  snack: "food",
  snack_shop: "food",
  dessert: "food",
  bakery: "food",
  milk_tea: "food",
  museum: "museum",
  culture: "museum",
  attraction: "sight",
  scenic: "sight",
  scenic_spot: "sight",
  landmark: "sight",
  business_area: "street",
  market: "market",
  photo_spot: "photo",
};

export const STOP_KIND_LABEL: Record<StopKind, string> = {
  park: "公园",
  food: "美食",
  museum: "博物馆",
  sight: "景点",
  street: "街区",
  market: "市集",
  photo: "打卡点",
  other: "游览",
};

/** 卡片按地点类型换样子；后端没给或不认识的分类一律算“游览”。 */
export function resolveStopKind(category: string | null | undefined): StopKind {
  const key = category?.trim().toLowerCase() ?? "";
  return STOP_KIND_BY_CATEGORY[key] ?? "other";
}

/**
 * 右边详情的“推荐理由”：去空后按后端顺序逐条返回。
 * 真实数据是帖子摘下的长句，不适合压成卡片上的一行短词，所以卡片不放。
 */
export function resolvePlaceTopReasons(
  detail: PlaceDetail | null | undefined,
): string[] {
  if (!detail || !Array.isArray(detail.top_reasons)) return [];
  return detail.top_reasons.map((item) => item.trim()).filter(Boolean);
}

/** 按 position 升序排列地点实拍图册。 */
export function sortPlaceGallery(
  gallery: readonly PlaceGalleryImage[] | null | undefined,
): PlaceGalleryImage[] {
  if (!gallery || gallery.length === 0) return [];
  return [...gallery].sort((a, b) => a.position - b.position);
}

/**
 * 正文段落：brief + activity_note，另加地点详情 summary。
 * 右边详情传 withBrief:false——brief 是帖子推荐理由第一条，那里已在“推荐理由”里逐条列出。
 */
export function resolveStopDetailBody(
  place: TripPlace,
  detail: PlaceDetail | null | undefined,
  { withBrief = true }: { withBrief?: boolean } = {},
): { itineraryText: string; detailSummary: string | null } {
  const brief = withBrief ? cleanBrief(place.brief) : "";
  const note = place.activity_note?.trim() ?? "";

  let itineraryText = "";
  if (brief && note && brief !== note) {
    const endsWithPunct = /[。！？.!?;；]$/.test(brief);
    itineraryText = `${brief}${endsWithPunct ? "" : "。"}${note}`;
  } else {
    itineraryText = note || brief;
  }

  const rawSummary = detail?.summary?.trim() ?? "";
  const detailSummary =
    rawSummary && rawSummary !== itineraryText ? rawSummary : null;

  return {
    itineraryText,
    detailSummary,
  };
}

/** 在当天 commute_legs 里按起终点 ID 精确找这一段通勤。 */
export function findCommuteLeg(
  day: TripDay | null | undefined,
  fromPlaceId: number,
  toPlaceId: number,
): CommuteLeg | undefined {
  if (!day || !Array.isArray(day.commute_legs)) return undefined;
  return day.commute_legs.find(
    (leg) => leg.from_place_id === fromPlaceId && leg.to_place_id === toPlaceId,
  );
}

/**
 * 右边详情“到下一站”的通勤：后端 transit_summary 里已含分钟，有就只写它；
 * 没有才写“步行 · 10 分钟”，估算再加“估算”。
 */
export function formatNextLegText(leg: CommuteLeg): string {
  const summary = leg.transit_summary?.trim();
  if (summary) return summary;
  return [
    commuteModeName(leg.mode),
    `${leg.duration_minutes} 分钟`,
    leg.duration_source === "estimate" ? "估算" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export type CommutePopupContent =
  | { kind: "steps"; steps: TransitStep[] }
  | { kind: "summary"; summary: string }
  | null;

/**
 * 站间通勤明细弹窗内容：
 * 1. 有 transit_steps 时逐段写线路、上车站、分钟；
 * 2. 没有时写 transit_summary；
 * 3. 都没有返回 null（不弹出）。
 */
export function resolveCommutePopup(leg: CommuteLeg): CommutePopupContent {
  if (Array.isArray(leg.transit_steps) && leg.transit_steps.length > 0) {
    return { kind: "steps", steps: leg.transit_steps };
  }
  const summary = leg.transit_summary?.trim();
  if (summary) {
    return { kind: "summary", summary };
  }
  return null;
}

/** 单段换乘步骤的可读文本：覆盖线路、上车站、下车站、分钟。 */
export function formatTransitStepLine(step: TransitStep): string {
  const parts: string[] = [];
  if (step.line_name?.trim()) {
    parts.push(step.line_name.trim());
    if (step.from_stop?.trim() && step.to_stop?.trim()) {
      parts.push(`${step.from_stop.trim()}上车 → ${step.to_stop.trim()}`);
    } else if (step.from_stop?.trim()) {
      parts.push(`${step.from_stop.trim()}上车`);
    }
    if (step.stop_count != null) {
      parts.push(`${step.stop_count} 站`);
    }
  } else {
    parts.push(commuteModeName(step.kind));
  }
  if (step.duration_minutes != null) {
    parts.push(`${step.duration_minutes} 分钟`);
  }
  return parts.join(" · ");
}

/** 书名卡摘要 + #标签整理。 */
export function resolveBookCardCopy(
  summary: string | null | undefined,
  tags: readonly string[] | null | undefined,
): { summary: string; tags: string[] } {
  const cleanedSummary = cleanSummary(summary);
  const cleanedTags = cleanTags([...(tags ?? [])]).map(
    (t) => `#${t.replace(/^#+/, "")}`,
  );
  return {
    summary: cleanedSummary,
    tags: cleanedTags,
  };
}

export function isValidGeoPoint(
  longitude: number | null | undefined,
  latitude: number | null | undefined,
): boolean {
  return (
    typeof longitude === "number" &&
    typeof latitude === "number" &&
    Number.isFinite(longitude) &&
    Number.isFinite(latitude) &&
    Math.abs(longitude) <= 180 &&
    Math.abs(latitude) <= 90 &&
    (longitude !== 0 || latitude !== 0)
  );
}

export function hasValidAccommodationCoords(
  accommodation: AccommodationInfo | null | undefined,
): accommodation is AccommodationInfo {
  return Boolean(
    accommodation &&
    isValidGeoPoint(accommodation.longitude, accommodation.latitude),
  );
}

export const DEFAULT_BOOK_STRIP_HEIGHT = 215;
export const DEFAULT_BOOK_STRIP_FOLDED_HEIGHT = 53;

export interface BookMapAvoidMetrics {
  /** 长卷当前可见高度（px）；未传或 <= 0 时回落到默认估计高度 */
  stripVisibleHeight?: number;
  /** 长卷是否处于贴底收起态（贴底时底部不再有 16px 外边距） */
  stripFolded?: boolean;
  /** 右栏实际宽度（px）；未传或 <= 0 时按 isReading / hasSidePanel 推导 */
  sideWidth?: number;
}

/**
 * 计算高德 setFitView 避让边距 [top, bottom, left, right]：
 * 避让左上书名卡（左留 380）、底部长卷（按长卷当前可见高度 + 16 + 44，贴底收起不加 16，阅读模式留 70）、
 * 右边抽屉（按右栏实际宽度 + 20 + 40，无右栏留 140），
 * 并在小屏幕时等比收敛，确保中间留出至少 120×120 可视视野。
 */
export function computeBookMapAvoidPadding(
  viewportWidth: number,
  viewportHeight: number,
  hasSidePanel: boolean,
  isReading = false,
  metrics?: BookMapAvoidMetrics,
): [number, number, number, number] {
  const w =
    Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 1440;
  const h =
    Number.isFinite(viewportHeight) && viewportHeight > 0
      ? viewportHeight
      : 848;

  const stripFolded = Boolean(metrics?.stripFolded);
  const measuredStripH = metrics?.stripVisibleHeight;
  const stripVisibleH =
    typeof measuredStripH === "number" &&
    Number.isFinite(measuredStripH) &&
    measuredStripH > 0
      ? measuredStripH
      : stripFolded
        ? DEFAULT_BOOK_STRIP_FOLDED_HEIGHT
        : DEFAULT_BOOK_STRIP_HEIGHT;

  const measuredSideW = metrics?.sideWidth;
  const resolvedSideW =
    !isReading && !hasSidePanel
      ? 0
      : typeof measuredSideW === "number" &&
          Number.isFinite(measuredSideW) &&
          measuredSideW > 0
        ? measuredSideW
        : isReading
          ? Math.max(0, Math.min(540, w - 380))
          : 380;

  const topTarget = 90;
  const bottomTarget = isReading
    ? 70
    : stripVisibleH + (stripFolded ? 0 : 16) + 44;
  const leftTarget = 380;
  const rightTarget = resolvedSideW > 0 ? resolvedSideW + 20 + 40 : 140;

  const minClearW = 120;
  const maxHoriz = Math.max(40, w - minClearW);
  const horizSum = leftTarget + rightTarget;
  const hScale = horizSum > maxHoriz ? maxHoriz / horizSum : 1;

  const minClearH = 120;
  const maxVert = Math.max(40, h - minClearH);
  const vertSum = topTarget + bottomTarget;
  const vScale = vertSum > maxVert ? maxVert / vertSum : 1;

  return [
    Math.round(topTarget * vScale),
    Math.round(bottomTarget * vScale),
    Math.round(leftTarget * hScale),
    Math.round(rightTarget * hScale),
  ];
}

export function isReducedMotionPreferred(): boolean {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return false;
  }
  return Boolean(
    window.matchMedia("(prefers-reduced-motion: reduce)")?.matches,
  );
}

/* ---------- Step 7b + 7c: 右上一排、没排进来、住处、五块抽屉与全程攻略 ---------- */

export type BookBarPageId = "guide" | "before" | "go" | "cost";
export type BookSidePageId = BookBarPageId | "miss" | "stay";

/** 右上一排四页（攻略 / 出发前 / 怎么去 · 住哪 / 花多少）共享同一种阅读模式右栏。 */
export function isBookReadingPage(page: BookSidePageId | null): boolean {
  return (
    page === "guide" || page === "before" || page === "go" || page === "cost"
  );
}

/** 换天时关上站点详情与“住处”；攻略 / 出发前 / 怎么去·住哪 / 花多少 / 没排进来的留在右边。 */
export function shouldKeepSidePageOnDaySwitch(
  page: BookSidePageId | null,
): boolean {
  return (
    page === "guide" ||
    page === "before" ||
    page === "go" ||
    page === "cost" ||
    page === "miss"
  );
}

export interface ReadingIntersectionSample {
  placeId: number;
  top: number;
  height: number;
}

/**
 * 当多站同时擦过阅读探测带时，选最贴近阅读参考线 targetTop 的那一站；
 * 若无布局尺寸（如部分测试环境全为 0），退回第一条命中项。
 */
export function pickClosestReadingStopId(
  samples: readonly ReadingIntersectionSample[],
  targetTop: number,
): number | null {
  if (samples.length === 0) return null;
  const hasGeometry = samples.some((s) => s.height > 0 || s.top !== 0);
  if (!hasGeometry) return samples[0].placeId;

  let bestId = samples[0].placeId;
  let bestDist = Infinity;
  for (const s of samples) {
    const center = s.top + s.height * 0.35;
    const dist =
      targetTop >= s.top && targetTop <= s.top + s.height
        ? Math.abs(targetTop - center) * 0.25
        : Math.abs(targetTop - center);
    if (dist < bestDist) {
      bestDist = dist;
      bestId = s.placeId;
    }
  }
  return bestId;
}

export interface GuideScrollMetrics {
  asideTop: number;
  asideHeight: number;
  scrollTop: number;
  scrollHeight: number;
}

/**
 * 攻略一页一天（底部留白收紧到 28px）时的滚读站点判定：
 * 常规区间沿用顶部 28% 阅读线；接近页面底部时将阅读线平滑下移至视口下方，
 * 到达底部时直接落到最后一站，保证最后一站无需 58vh 空白也能被判定为“正在读”。
 */
export function pickGuideReadingStopId(
  samples: readonly ReadingIntersectionSample[],
  metrics: GuideScrollMetrics,
): number | null {
  if (samples.length === 0) return null;
  if (samples.length === 1) return samples[0].placeId;

  const { asideTop, asideHeight, scrollTop, scrollHeight } = metrics;
  const maxScroll = Math.max(0, scrollHeight - asideHeight);
  if (maxScroll <= 1) {
    return samples[0].placeId;
  }
  if (scrollTop <= 2) {
    return samples[0].placeId;
  }
  if (scrollTop >= maxScroll - 4) {
    return samples[samples.length - 1].placeId;
  }

  const baseLine = asideTop + asideHeight * 0.28;
  const bottomLine = asideTop + asideHeight * 0.85;
  const ramp = Math.min(maxScroll, asideHeight * 0.45);
  const remaining = Math.max(0, maxScroll - scrollTop);
  const progress = ramp > 0 && remaining < ramp ? 1 - remaining / ramp : 0;
  const readingLineY = baseLine + (bottomLine - baseLine) * progress;

  let current: number | null = null;
  for (const s of samples) {
    if (s.top <= readingLineY) {
      current = s.placeId;
    }
  }
  return current ?? samples[0].placeId;
}

/** 筛出未排入行程的必去地点（同 MustIncludeNotice.tsx 第 11-18 行）。 */
export function filterUnscheduledMustInclude(
  items: readonly TripMustInclude[] | null | undefined,
): TripMustInclude[] {
  if (!items || items.length === 0) return [];
  return items.filter((item) => item.status !== "scheduled");
}

/** 必去地点未排入状态后缀（同 MustIncludeNotice.tsx 第 35-42 行）。 */
export function formatMustIncludeStatusSuffix(
  status: TripMustInclude["status"],
): string {
  switch (status) {
    case "cross_city":
      return " (不在本城市)";
    case "recorded_unmatched":
      return " (未匹配到对应地点)";
    case "recorded_candidate":
      return " (候选地点备用)";
    default:
      return "";
  }
}

/** weather.status 非 ok 时的提示（复制自 WeatherCard.tsx 第 26-31 行未导出的 STATUS_HINT）。 */
export const WEATHER_STATUS_HINT: Record<string, string> = {
  skipped_date_out_of_range: "出发日期较远，临近出行时可查看天气预报",
  skipped_disabled: "天气服务暂未开启",
  failed: "天气数据获取失败，请稍后再试",
};

export function getWeatherUnavailableHint(
  status: string | null | undefined,
): string {
  return (status && WEATHER_STATUS_HINT[status]) || "暂无天气数据";
}

/** 将 weather.days[].date ("YYYY-MM-DD") 转为 "11月14日"，按字符串分段避免时区偏移。 */
export function formatWeatherMonthDay(
  dateStr: string | null | undefined,
): string | null {
  const trimmed = dateStr?.trim();
  if (!trimmed) return null;
  const match = /^\d{4}-0?(\d{1,2})-0?(\d{1,2})(?:$|T)/.exec(trimmed);
  if (match) {
    return `${Number(match[1])}月${Number(match[2])}日`;
  }
  return trimmed;
}

export type BeforeWeatherDayItem =
  | {
      kind: "forecast";
      day: number;
      title: string;
      Icon: LucideIcon;
      weatherText: string;
      tempRange: string;
      windText: string | null;
      reminders: string[];
    }
  | {
      kind: "missing";
      day: number;
      title: string;
    };

export type BeforeWeatherSection =
  | { ok: true; items: BeforeWeatherDayItem[] }
  | { ok: false; hint: string };

/** 出发前·天气展示模型：仅 status === "ok" 按 plan.days 逐天渲染，其余给一句状态提示。 */
export function resolveBeforeWeatherSection(
  days: readonly TripDay[],
  weather: TripWeather | null | undefined,
): BeforeWeatherSection {
  if (!weather || weather.status !== "ok") {
    return {
      ok: false,
      hint: getWeatherUnavailableHint(weather?.status),
    };
  }

  const items: BeforeWeatherDayItem[] = days.map((d) => {
    const dayLabel = formatBookDayLabel(d.day);
    const w = getDayWeather(weather, d.day);
    if (!w) {
      return {
        kind: "missing",
        day: d.day,
        title: `${dayLabel} · 暂无预报`,
      };
    }
    const dateLabel = formatWeatherMonthDay(w.date);
    return {
      kind: "forecast",
      day: d.day,
      title: dateLabel ? `${dayLabel} · ${dateLabel}` : dayLabel,
      Icon: weatherFaIcon(w.icon_code),
      weatherText: w.weather_text,
      tempRange: `${w.temp_min_c}–${w.temp_max_c}°C`,
      windText: w.wind_text?.trim() || null,
      reminders: collectWeatherReminders([w]),
    };
  });

  return { ok: true, items };
}

/** 大交通耗时格式化（复制自 TransportCard.tsx 第 11-18 行未导出的 formatDuration）。 */
export function formatTransportDuration(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes <= 0) return "0分";
  const rounded = Math.round(minutes);
  const hrs = Math.floor(rounded / 60);
  const mins = rounded % 60;
  if (hrs === 0) return `${mins}分`;
  if (mins === 0) return `${hrs}小时`;
  return `${hrs}小时${mins}分`;
}

/** 余票查询时间格式化（复制自 TransportCard.tsx 第 20-33 行未导出的 formatCheckedTime）。 */
export function formatTransportCheckedTime(
  utcIso: string | null | undefined,
): string | null {
  if (!utcIso) return null;
  try {
    const d = new Date(utcIso);
    if (Number.isNaN(d.getTime())) return null;
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const hours = String(d.getHours()).padStart(2, "0");
    const mins = String(d.getMinutes()).padStart(2, "0");
    return `${month}-${day} ${hours}:${mins}`;
  } catch {
    return null;
  }
}

/** 从各交通方式中取最晚的 availability_checked_at（同 TransportCard.tsx 第 194-199 行）。 */
export function getLatestTransportCheckedTime(
  transport: TripTransport | null | undefined,
): string | null {
  if (!transport || !Array.isArray(transport.modes)) return null;
  const checkedIsoList = transport.modes
    .map((m) => m.availability_checked_at)
    .filter((t): t is string =>
      Boolean(t && !Number.isNaN(new Date(t).getTime())),
    )
    .sort((a, b) => new Date(a).getTime() - new Date(b).getTime());
  const latest =
    checkedIsoList.length > 0
      ? checkedIsoList[checkedIsoList.length - 1]
      : null;
  return formatTransportCheckedTime(latest);
}

/** 大交通末尾来源提示（同 TransportCard.tsx 第 261-265 行）。 */
export function resolveTransportFooterNote(transport: TripTransport): string {
  return transport.source === "static_fallback" || transport.query_date === null
    ? "静态估算，班次与价格以购票平台为准"
    : "实时推荐方案";
}

function formatLodgingApproxRange(
  low: number | null | undefined,
  high: number | null | undefined,
  unit: string,
): string | null {
  const hasLow = low != null;
  const hasHigh = high != null;
  if (hasLow && hasHigh) {
    return low === high ? `约${low}${unit}` : `约${low}–${high}${unit}`;
  }
  if (hasLow) return `约${low}${unit}`;
  if (hasHigh) return `约${high}${unit}`;
  return null;
}

function hasLodgingNightlyPrice(group: LodgingDisplayGroup): boolean {
  return group.nightly_low_cny != null && group.nightly_high_cny != null;
}

export interface LodgingTierTile {
  kind: LodgingDisplayGroup["kind"];
  label: string;
  priceText: string | null;
  hotelsText: string | null;
}

export type BookLodgingPresentation =
  | {
      kind: "legacy";
      title: string;
      reason: string | null;
    }
  | {
      kind: "user_lodging";
      text: string;
    }
  | {
      kind: "display";
      areaLabel: string;
      reason: string | null;
      caption: string | null;
      unmatchedNotices: LodgingDisplayNotice[];
      holidayNotices: LodgingDisplayNotice[];
      tiers: LodgingTierTile[];
      showPriceNote: boolean;
    };

/**
 * 住哪 / 住处展示数据：规则对齐 src/pages/mobile/MobileLodgingRow.tsx（按要求独立编写，不从手机端 import）。
 * 区别仅在于桌面抽屉按原型 .gtier 直接平铺酒店档次格，不做手机端折叠按钮。
 */
export function resolveBookLodgingPresentation(
  accommodation: AccommodationInfo | null | undefined,
): BookLodgingPresentation | null {
  if (!accommodation) return null;

  const display = accommodation.display;
  if (display == null) {
    return {
      kind: "legacy",
      title: accommodation.name,
      reason: accommodation.reason?.trim() || null,
    };
  }

  if (display.layout === "user_lodging") {
    const range = formatLodgingApproxRange(
      display.round_trip_minutes_low,
      display.round_trip_minutes_high,
      "分钟",
    );
    const head = `从你住的${display.area_label}出发`;
    return {
      kind: "user_lodging",
      text: range ? `${head}，每天来回${range}` : head,
    };
  }

  const groups = display.groups ?? [];
  const notices = display.notices ?? [];
  const unmatchedNotices = notices.filter(
    (n) => n.kind === "user_lodging_unmatched" && Boolean(n.text?.trim()),
  );
  const holidayNotices = notices.filter(
    (n) => n.kind === "holiday" && Boolean(n.text?.trim()),
  );

  const tiers: LodgingTierTile[] = groups.map((group) => {
    const hotelsText =
      (group.hotels ?? [])
        .map((h) => h.trim())
        .filter(Boolean)
        .join("、") || null;
    const priceText = hasLodgingNightlyPrice(group)
      ? formatLodgingApproxRange(
          group.nightly_low_cny,
          group.nightly_high_cny,
          "元/晚",
        )
      : null;
    return {
      kind: group.kind,
      label: group.label,
      priceText,
      hotelsText,
    };
  });

  return {
    kind: "display",
    areaLabel: display.area_label || accommodation.name,
    reason: accommodation.reason?.trim() || null,
    caption: display.caption?.trim() || null,
    unmatchedNotices,
    holidayNotices,
    tiers,
    showPriceNote: groups.some(hasLodgingNightlyPrice),
  };
}

/** 费用分类顺序与叫法（同 CostEstimateCard.tsx 第 23-45 行未导出的 ALL_CATEGORIES / CATEGORY_META）。 */
export const COST_CATEGORY_ITEMS: readonly {
  key: CostCategoryKey;
  label: string;
}[] = [
  { key: "intercity_transport", label: "大交通" },
  { key: "accommodation", label: "住宿" },
  { key: "local_transport", label: "市内交通" },
  { key: "admission", label: "景点门票" },
  { key: "meals", label: "餐饮" },
];

/** 默认场景：多个场景时优先选 scenario_id 含 train 的那个，没有则选第一个（同 PlanDetailPage.tsx 第 283-290 行）。 */
export function pickDefaultCostScenarioId(
  scenarios: readonly CostScenarioSummary[] | null | undefined,
): string | null {
  if (!scenarios || scenarios.length === 0) return null;
  const trainScenario = scenarios.find((s) => s.scenario_id.includes("train"));
  return trainScenario?.scenario_id ?? scenarios[0]?.scenario_id ?? null;
}

/** 费用明细来源小字（同 CostEstimateCard.tsx 第 332-341 行）。 */
export function resolveCostPriceBasisLabel(
  basis: CostPriceBasis | undefined,
): string | null {
  switch (basis) {
    case "sourced":
      return "来源查价";
    case "reference":
      return "参考估算";
    case "mixed":
      return "来源与参考混合";
    case "policy_zero":
      return "按规则计为 ¥0";
    default:
      return null;
  }
}

export interface BookCostCategoryRow {
  key: CostCategoryKey;
  label: string;
  amountText: string;
  /** 结构化分项峰值金额（已定价取 max_cny、policy_zero 取 0、未定价取 null），供比例条直接计算。 */
  amountCny: number | null;
  isUnpriced: boolean;
  basisLabel: string | null;
}

/** 费用明细五分类行模型：按大交通、住宿、市内交通、景点门票、餐饮顺序生成，金额取 max_cny。 */
export function resolveBookCostCategoryRows(
  scenario: CostScenarioSummary,
): BookCostCategoryRow[] {
  return COST_CATEGORY_ITEMS.map(({ key, label }) => {
    const cat = scenario.categories.find((c) => c.category === key);
    const isPolicyZero = cat?.price_basis === "policy_zero";
    const isPriced =
      cat?.coverage === "priced" && Boolean(cat.range) && !isPolicyZero;

    let amountText = "待确认";
    let amountCny: number | null = null;
    let isUnpriced = true;
    if (isPolicyZero) {
      amountText = "¥ 0";
      amountCny = 0;
      isUnpriced = false;
    } else if (isPriced && cat?.range) {
      amountText = `¥ ${cat.range.max_cny.toLocaleString()}`;
      amountCny = cat.range.max_cny;
      isUnpriced = false;
    }

    return {
      key,
      label,
      amountText,
      amountCny,
      isUnpriced,
      basisLabel: resolveCostPriceBasisLabel(cat?.price_basis),
    };
  });
}

export type CostBarTone =
  | "deep"
  | "green"
  | "morning"
  | "afternoon"
  | "evening";

export const COST_BAR_TONES: readonly CostBarTone[] = [
  "deep",
  "green",
  "morning",
  "afternoon",
  "evening",
];

/**
 * 将 getScenarioCostStatus 返回的 costText 拆成“前导状态字 + 主金额”两段展示，不自行拼金额。
 * 例：“预估 ¥ 1,800” -> { prefix: "预估", main: "¥ 1,800" }；
 * 不可估（showAmount === false）-> { prefix: null, main: "费用暂不可估算" }。
 */
export function splitScenarioCostText(
  status: Pick<ScenarioCostStatus, "costText" | "showAmount">,
): { prefix: string | null; main: string } {
  const raw = status.costText.trim();
  if (!status.showAmount) {
    return { prefix: null, main: raw };
  }
  const spaceIdx = raw.indexOf(" ");
  if (spaceIdx <= 0) {
    return { prefix: null, main: raw };
  }
  return {
    prefix: raw.slice(0, spaceIdx),
    main: raw.slice(spaceIdx + 1).trim(),
  };
}

export interface BookCostBarSegment {
  key: CostCategoryKey;
  tone: CostBarTone;
  amountCny: number;
  ratio: number;
}

/**
 * 仅按已定价且金额 > 0 的分项生成比例条与行色块映射，颜色依次取 --deep、--green、--dot-morning、--dot-afternoon（第五项兜底 --dot-evening）。
 */
export function resolveBookCostBreakdownVisuals(
  rows: readonly BookCostCategoryRow[],
): {
  segments: BookCostBarSegment[];
  toneByKey: Partial<Record<CostCategoryKey, CostBarTone>>;
} {
  const pricedRows = rows.filter(
    (row): row is BookCostCategoryRow & { amountCny: number } =>
      !row.isUnpriced && typeof row.amountCny === "number" && row.amountCny > 0,
  );
  const sum = pricedRows.reduce((acc, row) => acc + row.amountCny, 0);
  if (sum <= 0) {
    return { segments: [], toneByKey: {} };
  }

  const toneByKey: Partial<Record<CostCategoryKey, CostBarTone>> = {};
  const segments: BookCostBarSegment[] = pricedRows.map((row, idx) => {
    const tone = COST_BAR_TONES[idx % COST_BAR_TONES.length];
    toneByKey[row.key] = tone;
    return {
      key: row.key,
      tone,
      amountCny: row.amountCny,
      ratio: row.amountCny / sum,
    };
  });

  return { segments, toneByKey };
}

/** 预估日期格式化（同 CostEstimateCard.tsx 第 47-59 行未导出的 formatEstimatedAt）。 */
export function formatCostEstimatedDate(
  isoString: string | null | undefined,
): string | null {
  if (!isoString) return null;
  try {
    const d = new Date(isoString);
    if (Number.isNaN(d.getTime())) return null;
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  } catch {
    return null;
  }
}

/* ---------- Step 7d: 地图上的天气与晴天掉太阳 ---------- */

export type BookMapWeatherKind =
  | "sun"
  | "cloud"
  | "rain"
  | "snow"
  | "fog"
  | "none";

export interface BookMapWeatherVisual {
  kind: BookMapWeatherKind;
  heavy: boolean;
}

const WEATHER_NONE_VISUAL: BookMapWeatherVisual = {
  kind: "none",
  heavy: false,
};

/** 仅按后端 icon_code 精确小写的映射表决定地图天气效果，不猜 weather_text。 */
export const WEATHER_ICON_VISUAL_MAP: Readonly<
  Record<string, BookMapWeatherVisual>
> = {
  sunny: { kind: "sun", heavy: false },
  clear: { kind: "sun", heavy: false },
  partly_cloudy: { kind: "cloud", heavy: false },
  cloudy: { kind: "cloud", heavy: false },
  overcast: { kind: "cloud", heavy: false },
  light_rain: { kind: "rain", heavy: false },
  rain: { kind: "rain", heavy: false },
  moderate_rain: { kind: "rain", heavy: false },
  heavy_rain: { kind: "rain", heavy: true },
  thunderstorm: { kind: "rain", heavy: true },
  thunder: { kind: "rain", heavy: true },
  light_snow: { kind: "snow", heavy: false },
  snow: { kind: "snow", heavy: false },
  sleet: { kind: "snow", heavy: false },
  heavy_snow: { kind: "snow", heavy: true },
  fog: { kind: "fog", heavy: false },
  haze: { kind: "fog", heavy: false },
  windy: WEATHER_NONE_VISUAL,
  unknown: WEATHER_NONE_VISUAL,
};

export function resolveBookMapWeatherVisual(
  iconCode: string | null | undefined,
): BookMapWeatherVisual {
  if (typeof iconCode !== "string") return WEATHER_NONE_VISUAL;
  const key = iconCode.trim().toLowerCase();
  if (!key) return WEATHER_NONE_VISUAL;
  return WEATHER_ICON_VISUAL_MAP[key] ?? WEATHER_NONE_VISUAL;
}

export interface SunDropPoint {
  x: number;
  y: number;
}

export interface SunDropBounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

export interface SunDropPlacement {
  start: SunDropPoint;
  target: SunDropPoint;
  bounds: SunDropBounds;
}

const SUN_DROP_BUTTON_RADIUS = 38;
export const SUN_DROP_CANDIDATE_COUNT = 14;

/** 按地图避让边距计算晴天太阳允许落点的安全区域，确保不落到左上卡、右上一排、右边抽屉或底部长卷下。 */
export function computeSunDropBounds(
  stageWidth: number,
  stageHeight: number,
  hasSidePanel: boolean,
  isReading = false,
): SunDropBounds {
  const w = Number.isFinite(stageWidth) && stageWidth > 0 ? stageWidth : 1440;
  const h = Number.isFinite(stageHeight) && stageHeight > 0 ? stageHeight : 848;

  const [padTop, padBottom, padLeft, padRight] = computeBookMapAvoidPadding(
    w,
    h,
    hasSidePanel,
    isReading,
  );

  const clearLeft = Math.max(0, Math.min(w, padLeft));
  const clearRight = Math.max(clearLeft, w - padRight);
  const clearTop = Math.max(Math.min(74, h), Math.min(h, padTop));
  const clearBottom = Math.max(clearTop, h - padBottom);

  const clearWidth = clearRight - clearLeft;
  const clearHeight = clearBottom - clearTop;
  const insetX =
    clearWidth >= SUN_DROP_BUTTON_RADIUS * 2.5
      ? SUN_DROP_BUTTON_RADIUS
      : clearWidth * 0.25;
  const insetY =
    clearHeight >= SUN_DROP_BUTTON_RADIUS * 2.5
      ? SUN_DROP_BUTTON_RADIUS
      : clearHeight * 0.25;

  const minX = Math.round(clearLeft + insetX);
  const maxX = Math.max(minX, Math.round(clearRight - insetX));
  const minY = Math.round(clearTop + insetY);
  const maxY = Math.max(minY, Math.round(clearBottom - insetY));

  return { minX, maxX, minY, maxY };
}

function halton(index: number, base: number): number {
  let result = 0;
  let f = 1 / base;
  let i = index;
  while (i > 0) {
    result += f * (i % base);
    i = Math.floor(i / base);
    f /= base;
  }
  return result;
}

/**
 * 在避让区域内撒 14 个候选点，挑离当天站点钉和住处最远的一点落太阳；
 * 同时给出舞台上沿外的入场起点与最终目标坐标，供掉落与无障碍静止态复用。
 */
export function pickSunDropPlacement(
  stageWidth: number,
  stageHeight: number,
  hasSidePanel: boolean,
  isReading: boolean,
  avoidPoints: readonly SunDropPoint[],
  dayNumber = 1,
): SunDropPlacement {
  const bounds = computeSunDropBounds(
    stageWidth,
    stageHeight,
    hasSidePanel,
    isReading,
  );
  const spanX = bounds.maxX - bounds.minX;
  const spanY = bounds.maxY - bounds.minY;
  const daySeed = Number.isFinite(dayNumber) ? Math.max(0, dayNumber - 1) : 0;
  const shiftX = (daySeed * 0.17) % 1;
  const shiftY = (daySeed * 0.23) % 1;

  const candidates: SunDropPoint[] = [];
  for (let i = 1; i <= SUN_DROP_CANDIDATE_COUNT; i += 1) {
    const rx = (halton(i, 2) + shiftX) % 1;
    const ry = (halton(i, 3) + shiftY) % 1;
    candidates.push({
      x: Math.round(bounds.minX + rx * spanX),
      y: Math.round(bounds.minY + ry * spanY),
    });
  }

  let best = candidates[0] ?? {
    x: Math.round((bounds.minX + bounds.maxX) / 2),
    y: Math.round((bounds.minY + bounds.maxY) / 2),
  };

  if (avoidPoints.length > 0) {
    let bestScore = -1;
    for (const candidate of candidates) {
      let minDistSq = Infinity;
      for (const pt of avoidPoints) {
        if (!Number.isFinite(pt.x) || !Number.isFinite(pt.y)) continue;
        const dx = candidate.x - pt.x;
        const dy = candidate.y - pt.y;
        const dSq = dx * dx + dy * dy;
        if (dSq < minDistSq) {
          minDistSq = dSq;
        }
      }
      if (minDistSq > bestScore) {
        bestScore = minDistSq;
        best = candidate;
      }
    }
  } else {
    best = {
      x: Math.round(bounds.minX + spanX * 0.56),
      y: Math.round(bounds.minY + spanY * 0.44),
    };
  }

  // 从舞台上沿之外正上方直落（舞台 overflow: hidden，太阳从顶边露头）
  return {
    start: { x: best.x, y: -(SUN_DROP_BUTTON_RADIUS + 22) },
    target: best,
    bounds,
  };
}

export interface WeatherReminderBubbleModel {
  headline: string;
  windLine: string | null;
  reminders: string[];
}

/** 构造晴天收太阳后的天气提醒卡片内容：原样输出 weather_text/wind_text，并通过 collectWeatherReminders 合并极端高温提示。 */
export function buildWeatherReminderContent(
  weatherDay: WeatherDay,
): WeatherReminderBubbleModel {
  const wind = weatherDay.wind_text?.trim() || null;
  return {
    headline: `${formatBookDayLabel(weatherDay.day)} · ${weatherDay.weather_text} ${weatherDay.temp_min_c}–${weatherDay.temp_max_c}°C`,
    windLine: wind,
    reminders: collectWeatherReminders([weatherDay]),
  };
}

export interface SunBubblePosition {
  left: number;
  top: number;
  placement: "above" | "below";
  arrowLeft: number;
}

/** 根据目标天气图标在 .bk-atlas 内的位置决定提醒卡往上或往下弹出，避免超出舞台裁切。 */
export function computeSunBubblePosition(
  iconRect: { left: number; right: number; top: number; bottom: number },
  stageRect: { left: number; top: number; width: number; height: number },
  bubbleWidth = 270,
): SunBubblePosition {
  const stageW = stageRect.width > 0 ? stageRect.width : 1440;
  const iconCenterX = (iconRect.left + iconRect.right) / 2 - stageRect.left;
  const relTop = iconRect.top - stageRect.top;
  const relBottom = iconRect.bottom - stageRect.top;

  const left = Math.round(
    Math.max(12, Math.min(stageW - bubbleWidth - 12, iconCenterX - 34)),
  );
  const arrowLeft = Math.round(
    Math.max(18, Math.min(bubbleWidth - 30, iconCenterX - left - 6)),
  );

  if (relTop >= 165) {
    return {
      left,
      top: Math.round(relTop - 12),
      placement: "above",
      arrowLeft,
    };
  }

  return {
    left,
    top: Math.round(relBottom + 12),
    placement: "below",
    arrowLeft,
  };
}
