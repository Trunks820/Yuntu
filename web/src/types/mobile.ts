import type { ConceptImageKey } from "@/assets/mobile-concepts/conceptImages";
import type { TripSubmitRequest } from "./tripRequest";

export type MobileStep =
  | "home"
  | "moments"
  | "rhythm"
  | "profile"
  | "departure"
  | "companions"
  | "review";

export type MobileSceneCategory = "street" | "nature" | "food" | "night";

export interface MobileScene {
  id: string;
  category: MobileSceneCategory;
  /** 卡片英文小标题 */
  en: string;
  /** 卡片主标题，"\n" 处换行 */
  title: string;
  /** 短名，用于无障碍标签 */
  short: string;
  /** 偏好回显标签（街区漫游 / 自然放松 / 当地美食 / 城市夜色） */
  tag: string;
  image: ConceptImageKey;
  interestKeyword: string;
  batchIndex: number;
}

export const SCENE_CATEGORY_INTEREST_MAP: Record<MobileSceneCategory, string> = {
  street: "citywalk",
  nature: "自然风光",
  food: "美食",
  night: "夜景",
};

/**
 * 原型中 12 张场景画面（文案、图片与原型 SCENES 一致），分 4 类，每批 4 张（各类 1 张）。
 * 换一批时已选卡片留在原位，只轮换未选位置；选满 4 张时禁用换一批。
 * id 沿用 street_1 这一套，以兼容已保存的草稿。
 */
export const MOBILE_SCENES: MobileScene[] = [
  // 批次 0
  {
    id: "street_1",
    category: "street",
    en: "STREETS & STORIES",
    title: "钻进老街，\n不赶时间",
    short: "老街的日常",
    tag: "街区漫游",
    image: "alley",
    interestKeyword: "citywalk",
    batchIndex: 0,
  },
  {
    id: "nature_1",
    category: "nature",
    en: "ROOM TO BREATHE",
    title: "在山水之间，\n留一段空白",
    short: "山水间发呆",
    tag: "自然放松",
    image: "lake",
    interestKeyword: "自然风光",
    batchIndex: 0,
  },
  {
    id: "food_1",
    category: "food",
    en: "A TASTE OF LIFE",
    title: "找一家小馆，\n坐下来慢慢吃",
    short: "街边小馆",
    tag: "当地美食",
    image: "cafe",
    interestKeyword: "美食",
    batchIndex: 0,
  },
  {
    id: "night_1",
    category: "night",
    en: "AFTER THE SUNSET",
    title: "等灯火亮起，\n再认识这座城",
    short: "城市的夜色",
    tag: "城市夜色",
    image: "night",
    interestKeyword: "夜景",
    batchIndex: 0,
  },
  // 批次 1
  {
    id: "street_2",
    category: "street",
    en: "A CITY WAKING UP",
    title: "趁街巷刚醒，\n随意走走",
    short: "清晨街巷",
    tag: "街区漫游",
    image: "rooftops",
    interestKeyword: "citywalk",
    batchIndex: 1,
  },
  {
    id: "nature_2",
    category: "nature",
    en: "UNDER THE TREES",
    title: "走进树荫，\n听风吹过",
    short: "树荫里的风",
    tag: "自然放松",
    image: "forest",
    interestKeyword: "自然风光",
    batchIndex: 1,
  },
  {
    id: "food_2",
    category: "food",
    en: "ONE LOCAL BITE",
    title: "尝一口当地味，\n再决定下一站",
    short: "当地的一口",
    tag: "当地美食",
    image: "food",
    interestKeyword: "美食",
    batchIndex: 1,
  },
  {
    id: "night_2",
    category: "night",
    en: "AFTER DINNER",
    title: "吃过晚饭，\n再沿着灯光走走",
    short: "晚饭后的散步",
    tag: "城市夜色",
    image: "night",
    interestKeyword: "夜景",
    batchIndex: 1,
  },
  // 批次 2
  {
    id: "street_3",
    category: "street",
    en: "TAKE THE SMALL TURN",
    title: "拐进小巷，\n看看会遇见什么",
    short: "小巷偶遇",
    tag: "街区漫游",
    image: "alley",
    interestKeyword: "citywalk",
    batchIndex: 2,
  },
  {
    id: "nature_3",
    category: "nature",
    en: "TIME BY THE WATER",
    title: "在水边坐坐，\n等天色慢慢变",
    short: "水边等天色",
    tag: "自然放松",
    image: "coast",
    interestKeyword: "自然风光",
    batchIndex: 2,
  },
  {
    id: "food_3",
    category: "food",
    en: "A TABLE FOR A WHILE",
    title: "找个街边座位，\n把一顿饭吃慢",
    short: "慢慢吃顿饭",
    tag: "当地美食",
    image: "cafe",
    interestKeyword: "美食",
    batchIndex: 2,
  },
  {
    id: "night_3",
    category: "night",
    en: "STAY FOR THE LIGHTS",
    title: "找个角落，\n看城市亮起来",
    short: "等一城灯火",
    tag: "城市夜色",
    image: "night",
    interestKeyword: "夜景",
    batchIndex: 2,
  },
];

export type MobileRhythm = "slow" | "full";

export interface RhythmOption {
  id: MobileRhythm;
  label: string;
  description: string;
  requestKeyword: string;
}

export const MOBILE_RHYTHM_OPTIONS: RhythmOption[] = [
  {
    id: "slow",
    label: "慢一点",
    description: "每天留白充裕，喝杯咖啡，随心逛逛",
    requestKeyword: "慢一点",
  },
  {
    id: "full",
    label: "充实一点",
    description: "安排精彩紧凑，想看更多风景与体验",
    requestKeyword: "充实一点",
  },
];

export type MobileCompanionRelation =
  | "solo"
  | "partner"
  | "friends"
  | "parents"
  | "kids";

export interface CompanionBoundary {
  label: string;
  defaultAdults: number;
  defaultChildren: number;
  minAdults: number;
  maxAdults: number;
  minChildren: number;
  maxChildren: number;
  maxTotal: number;
}

/**
 * 5 种同行关系上下边界与求和规则：
 * - solo: 1成人，0儿童（不可调整）
 * - partner: 2成人，0-3儿童
 * - friends: 2-6成人，0儿童
 * - parents: 默认3成人0儿童；成人>=2, 儿童0-3, 总人数<=6
 * - kids: 默认2成人1儿童；成人>=1, 儿童1-3, 总人数<=6
 */
export const COMPANION_BOUNDARIES: Record<MobileCompanionRelation, CompanionBoundary> = {
  solo: {
    label: "独自一人",
    defaultAdults: 1,
    defaultChildren: 0,
    minAdults: 1,
    maxAdults: 1,
    minChildren: 0,
    maxChildren: 0,
    maxTotal: 1,
  },
  partner: {
    label: "情侣/双人",
    defaultAdults: 2,
    defaultChildren: 0,
    minAdults: 2,
    maxAdults: 2,
    minChildren: 0,
    maxChildren: 3,
    maxTotal: 5,
  },
  friends: {
    label: "朋友同行",
    defaultAdults: 2,
    defaultChildren: 0,
    minAdults: 2,
    maxAdults: 6,
    minChildren: 0,
    maxChildren: 0,
    maxTotal: 6,
  },
  parents: {
    label: "带父母/长辈",
    defaultAdults: 3,
    defaultChildren: 0,
    minAdults: 2,
    maxAdults: 6,
    minChildren: 0,
    maxChildren: 3,
    maxTotal: 6,
  },
  kids: {
    label: "带孩子/亲子",
    defaultAdults: 2,
    defaultChildren: 1,
    minAdults: 1,
    maxAdults: 5,
    minChildren: 1,
    maxChildren: 3,
    maxTotal: 6,
  },
};

export type MobileAvoidId = "no_early" | "less_climb" | "less_queue" | "no_rush";

export interface MobileAvoidOption {
  id: MobileAvoidId;
  label: string;
  requestText: string;
}

/**
 * 4 项叮嘱，严格保持原文，不自作聪明映射为太累/爬山/人太多，也不自设 daily_start
 */
export const MOBILE_AVOID_OPTIONS: MobileAvoidOption[] = [
  { id: "no_early", label: "不想早起", requestText: "不想早起" },
  { id: "less_climb", label: "尽量少爬坡", requestText: "尽量少爬坡" },
  { id: "less_queue", label: "尽量少排队", requestText: "尽量少排队" },
  { id: "no_rush", label: "不想赶行程", requestText: "不想赶行程" },
];

/**
 * 手机端本地 UI 草稿结构 (版本 1)
 * 存放在 sessionStorage 中，key 为 "yuntu-mobile-draft-v1"
 */
export interface MobileMustIncludePlace {
  placeId: number;
  name: string;
}

/** 生成请求 must_include 上限，与 BFF poi-catalog/selection 一致 */
export const MOBILE_MUST_INCLUDE_LIMIT = 5;

export interface MobileTripDraft {
  version: 1;
  revision: number;
  step: MobileStep;
  /** quiz：从挑心动画面开始；direct：从首页「已经想好去哪」先选城；inspiration：从「随手收集灵感」带着想去的地点来 */
  entry: "quiz" | "direct" | "inspiration";
  /** 「随手收集灵感」带上的真实目录地点（最多 5 个，只属于 cityName 这座城） */
  mustInclude: MobileMustIncludePlace[];
  sceneIds: string[];
  momentBatch: number;
  seenSceneIds: string[];
  rhythm: MobileRhythm | null;
  cityId: string | null;
  cityName: string | null;
  cityConfirmed: boolean;
  days: number | null; // 1-7 或 null
  dateMode: "undecided" | "exact";
  startDate: string | null; // "YYYY-MM-DD"
  companion: MobileCompanionRelation;
  adults: number;
  children: number;
  avoidIds: MobileAvoidId[];
  rawNotes: string; // 用户输入的原始备注（最多 300 字），绝不包含自动追加的同行块
  editReturn: MobileStep | null;
  ownerUserId: string | null;
  updatedAt: number;
}

/**
 * 手机端提交暂存快照
 */
export interface MobileSubmission {
  owner: string;
  fingerprint: string;
  request_id: string;
  trip_request: TripSubmitRequest;
  created_at: number;
  draft_revision?: number;
  status: "pending_confirmation" | "confirmed" | "integrity_error";
  job_id?: string | null;
}


