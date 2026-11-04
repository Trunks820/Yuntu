import type { CostEstimateSummary } from "./cost";
export type { CostEstimateSummary };

export type PaceLevel = "RELAXED" | "MODERATE" | "INTENSIVE";
export type CommuteStatus = "WITHIN_LIMIT" | "OVER_LIMIT";

/**
 * 结果接口返回的出行方式（v1.4）：含历史兼容的 walking。
 * 不要用此类型约束创建行程的请求体，请用 RequestedCommuteMode。
 */
export type ResultCommuteMode = "driving" | "transit" | "walking" | "cycling";

/** @deprecated 改用 ResultCommuteMode（结果）或 form.ts 的 RequestedCommuteMode（请求） */
export type CommuteMode = ResultCommuteMode;

/** 公交 leg 内单段步骤的类型 */
export type TransitStepKind = "walking" | "bus" | "rail" | "other";

/** transit leg 内的单段步骤（mode=transit 时 transit_steps 数组元素） */
export interface TransitStep {
  kind: TransitStepKind;
  duration_minutes: number | null;
  distance_meters: number | null;
  line_name: string | null;
  provider_type: string | null; // "地铁线路" / "普通公交线路" 等，仅展示用，勿做程序判断
  from_stop: string | null;
  to_stop: string | null;
  stop_count: number | null;
}
/** 后端 role 取值较多（anchor_activity/secondary_activity/meal_stop/photo_stop/optional_stop…），保留为开放字符串 */
export type PlaceRole = string;

/* ---------- Schema 1.5 停留时段（v0.8.12 契约） ---------- */

/** 公开停留时段：上午/下午/傍晚/晚上。后端从路线推导，前端不得反推成钟点 */
export type SchedulePeriod = "morning" | "afternoon" | "evening" | "night";

/** 精确时间的受治理来源。只有这四种来源的 exact_* 才允许展示为确定时间 */
export type ExactTimeSource =
  | "reservation"
  | "event"
  | "transport"
  | "verified_venue_rule";

/**
 * place.schedule（Schema 1.5）。exact_start/exact_end 通常为 null；
 * 非空时必有 exact_time_source（后端 fail-closed 校验），但前端展示前仍须自行验证。
 */
export interface PlaceScheduleInfo {
  period: SchedulePeriod;
  exact_start?: string | null; // "HH:MM"
  exact_end?: string | null; // "HH:MM"
  exact_time_source?: ExactTimeSource | null;
}

/** must_include 项状态（v0.8.12：scheduled/not_scheduled 为交付状态，其余为记录状态） */
export type MustIncludeStatus =
  | "scheduled"
  | "not_scheduled"
  | "recorded_candidate"
  | "recorded_unmatched"
  | "cross_city";

/** 结果里的必去地点回执。not_scheduled 时 reason 为用户可读原因 */
export interface TripMustInclude {
  name: string;
  status: MustIncludeStatus;
  place_id?: number | null;
  reason?: string | null;
  matched_city?: string | null;
}

/** 结果回显的时间偏好。daily_start/daily_end 均为 null 即"无固定时间" */
export interface TripTimePreferences {
  daily_start?: string | null;
  daily_end?: string | null;
}
export type JobStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
export type StageCode = "ANALYZING" | "PLANNING" | "COMPOSING" | "FINALIZING";

export interface StageProgress {
  code: StageCode;
  step: number;
  total: number;
}

export interface JobError {
  code: string;
  message: string;
}

export interface JobResponse {
  ok: boolean;
  job_id: string;
  status: JobStatus;
  stage_progress: StageProgress | null;
  result_record_id: string | null;
  error: JobError | null;
}

export interface TripPlace {
  place_id: number;
  name: string;
  category: string;
  longitude: number;
  latitude: number;
  role: PlaceRole;
  optional: boolean;
  brief: string;
  stay_minutes?: number;
  /** Schema 1.5：本条路线中该地点的体验说明；1.4 结果无此字段 */
  activity_note?: string | null;
  /** Schema 1.5：停留时段与受治理精确时间；1.4 结果无此字段 */
  schedule?: PlaceScheduleInfo | null;
}

export interface PlaceGalleryVariant {
  url: string;
  width: number;
  height: number;
}

export interface PlaceGalleryImage {
  asset_id: string;
  position: number;
  alt_text: string;
  desktop: PlaceGalleryVariant;
  mobile: PlaceGalleryVariant;
  thumb: PlaceGalleryVariant;
}

/** GET /trip/places/{id} 响应。空字段前端按空态隐藏，后端不给假文案。 */
export interface PlaceDetail {
  place_id: number;
  name: string;
  place_type: string; // 与 TripPlace.category 同一套取值，复用 categoryIcon()
  district: string;
  longitude: number;
  latitude: number;
  summary: string;
  top_reasons: string[];
  warnings: string[];
  source_count: number;
  mention_count: number;
  gallery: PlaceGalleryImage[];
}

export interface CommuteLeg {
  from_place_id: number;
  to_place_id: number;
  mode: ResultCommuteMode;
  duration_source?: "amap" | "estimate";
  duration_minutes: number;
  distance_meters: number;
  encoded_polyline?: string;
  // 只在 mode === "transit" 时存在；明细缺失时为空数组
  transit_steps?: TransitStep[];
  // 后端生成的可直接展示文本；明细缺失时有保底文案
  transit_summary?: string;
}

export interface TripDay {
  day: number;
  title: string;
  places: TripPlace[];
  commute_legs: CommuteLeg[];
  commute_summary: string;
  pace_status: CommuteStatus;
  narrative: string;
}

export type AccommodationSource = "user_specified" | "auto_recommended";

export interface LodgingDisplayGroup {
  kind: string;
  label: string;
  hotels?: string[];
  nightly_low_cny?: number | null;
  nightly_high_cny?: number | null;
}

export interface LodgingDisplayNotice {
  kind: string;
  text: string;
}

export interface LodgingDisplay {
  layout: "user_lodging" | "homestay" | "tiers" | "brands" | "area_only";
  area_label: string;
  caption?: string;
  groups?: LodgingDisplayGroup[];
  notices?: LodgingDisplayNotice[];
  round_trip_minutes_low?: number | null;
  round_trip_minutes_high?: number | null;
  price_status?: "sourced" | "待核实";
}

export interface AccommodationInfo {
  name: string;
  latitude: number;
  longitude: number;
  source: AccommodationSource;
  reason?: string | null;
  display?: LodgingDisplay | null;
}

export interface PackingChecklistGroup {
  category: string;
  items: string[];
}

export interface TravelTip {
  title: string;
  content: string;
}

export interface TripPlan {
  plan_id: string;
  title: string;
  summary: string;
  tags: string[];
  pace: {
    level: PaceLevel;
    commute_status: CommuteStatus;
    total_commute_minutes: number;
  };
  accommodation?: AccommodationInfo | null;
  transport?: TripTransport | null;
  cost_estimate: CostEstimateSummary;
  days: TripDay[];
  /** Schema 2.2：行前必备清单（分类列表，每类最多3项，最多4类） */
  packing_checklist?: PackingChecklistGroup[] | null;
  /** Schema 2.2：实用避坑贴士（0～4条） */
  travel_tips?: TravelTip[] | null;
}

export interface TransportOption {
  type: "train" | "flight";
  no: string;
  departure_time: string;
  arrival_time: string;
  duration_minutes: number;
  price: string | null;
  departure_station: string;
  arrival_station: string;
  airline: string | null;
}

export interface TransportMode {
  mode: "train" | "flight";
  min_duration_minutes: number;
  price_range: string;
  price_source: "realtime" | "static_reference";
  daily_count: number;
  data_source: "realtime" | "static_fallback";
  availability_status: "available_at_query" | "sold_out_at_query" | "unknown";
  availability_checked_at: string | null;
  options: TransportOption[];
}

export interface TripTransport {
  from_city: string;
  to_city: string;
  query_date: string | null;
  source: "realtime" | "mixed" | "static_fallback";
  modes: TransportMode[];
}

/** 后端 weather.status：ok 为正常，其余（skipped_disabled / failed…）一律视为无数据 */
export type WeatherStatus = "ok" | string;

/** 与后端 weather.days[] 字段一一对应 */
export interface WeatherDay {
  day: number;
  date: string; // "2026-06-28"
  weather_text: string; // "中雨转大雨"
  temp_min_c: number;
  temp_max_c: number;
  wind_text: string; // "东风 1级"
  icon_code: string; // 见 constants/weather.ts 映射
  reminders: string[];
}

export interface TripWeather {
  status: WeatherStatus;
  city: string;
  days: WeatherDay[];
}

export interface TripResult {
  schema_version: string;
  /** Schema 2.1：公开交付形态。历史 Schema 2.0 结果可能缺省，按 normal 处理。 */
  published_variant?: "normal" | "safe";
  /** Schema 2.1：公开交付状态。历史 Schema 2.0 结果可能缺省，按 NORMAL 处理。 */
  delivery_status?: "NORMAL" | "DEGRADED";
  result_id: number;
  city: { name: string };
  request: {
    days: number;
    people_count: number;
    preferences: string[];
    avoid: string[];
  };
  weather?: TripWeather | null;
  /** Schema 1.5：请求时间偏好回显（无固定时间时两端均 null）；1.4 无此字段 */
  time_preferences?: TripTimePreferences | null;
  plans: TripPlan[];
  /** Schema 1.5：必去地点交付回执；1.4 无此字段 */
  must_include?: TripMustInclude[] | null;
}

export interface AsyncSubmitResponse {
  ok: boolean;
  job_id: string;
}

export interface ApiError {
  ok: false;
  error: {
    code: string;
    message: string;
  };
}

/* ---------- 分享图 / PDF 产物（后端统一 artifact 接口） ---------- */

export type ArtifactType = "pdf" | "share_image";
export type ArtifactStatus = "pending" | "running" | "ready" | "failed";

/** POST/GET /trip/results/{record_id}/artifacts/{type} 响应。ready 后才有 download_url */
export interface Artifact {
  ok: boolean;
  artifact_id: string;
  result_record_id: number;
  artifact_type: ArtifactType;
  status: ArtifactStatus;
  download_url: string;
  filename: string;
  mime_type: string;
  byte_size: number;
  page_count: number | null; // pdf ready 后有
  width_px: number | null; // share_image ready 后有
  height_px: number | null;
  expires_time: string;
  metadata: Record<string, unknown>;
  error: { code: string; message: string } | null;
}
