import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";
import paceActiveImg from "@/assets/spread-start/pace_active.webp";
import paceModerateImg from "@/assets/spread-start/pace_moderate.webp";
import paceSlowImg from "@/assets/spread-start/pace_slow.webp";
import {
  BUNDLED_FALLBACK_IMAGE,
  usesBundledCityAssets,
  withCityAssetBase,
} from "@/config/cityAssets";
import {
  CINEMATIC_COMMUTE_OPTIONS,
  CINEMATIC_RHYTHM_OPTIONS,
  type CinematicRhythm,
} from "@/types/cinematic";
import type { Destination } from "@/types/destination";
import type { RequestedCommuteMode } from "@/types/form";
import {
  COMPANION_BOUNDARIES,
  MOBILE_AVOID_OPTIONS,
  MOBILE_MUST_INCLUDE_LIMIT,
  MOBILE_SCENES,
  SCENE_CATEGORY_INTEREST_MAP,
  type MobileAvoidId,
  type MobileCompanionRelation,
  type MobileMustIncludePlace,
  type MobileSceneCategory,
} from "@/types/mobile";
import type { TripSubmitRequest } from "@/types/tripRequest";
import {
  CITY_NAME_TO_FOLDER,
  getCityGalleryItems,
  getCityMobilePhotoUrl,
  getCityPhotoCredit,
} from "@/utils/cityPhotos";
import {
  clampCompanionCounts,
  computeEndDate,
  formatCompanionNote,
  isDateInPast,
  isValidCalendarDate,
} from "@/utils/mobileDraft";
import { SCENE_PHOTOS_BY_ID } from "@/utils/scenePhotos.generated";
import {
  cleanTripCityName,
  pickNonGalleryDestinationPhoto,
} from "@/utils/tripShelf";

export const SPREAD_START_STEPS = ["心动", "节奏", "去哪", "日子与同行"] as const;
export const SPREAD_START_DAY_OPTIONS = [1, 2, 3, 4, 5, 6, 7] as const;

export const SPREAD_START_DRAFT_KEY = "yuntu-spread-start-draft-v1";
export const SPREAD_START_SUBMIT_INTENT_KEY = "yuntu-spread-start-submit-intent-v1";
export const SPREAD_START_NOTES_MAX = 300;
export const SPREAD_START_FROM_CITY_MAX = 60;
export const SPREAD_START_ACCOMMODATION_MAX = 120;

export type SpreadRhythmId = "slow" | "moderate" | "full";

export interface SpreadStartScenePhoto {
  fullUrl: string;
  smallUrl: string;
  credit: string;
}

export interface SpreadStartScene {
  id: string;
  category: MobileSceneCategory;
  title: string;
  short: string;
  tag: string;
  batch: 0 | 1;
  photos: SpreadStartScenePhoto[];
}

export interface SpreadStartRhythmOption {
  id: SpreadRhythmId;
  label: string;
  desc: string;
  requestPreference: CinematicRhythm;
  imageUrl: string;
}

export interface SpreadStartCommuteOption {
  id: RequestedCommuteMode;
  label: string;
  desc: string;
  icon: "car" | "bus" | "bike";
}

export interface SpreadStartCompanionOption {
  id: MobileCompanionRelation;
  line: string;
  title: string;
  sub: string;
  icon: "person" | "heart" | "people" | "family";
  wide?: boolean;
}

export interface SpreadStartDraft {
  version: 1;
  revision: number;
  step: 0 | 1 | 2 | 3;
  batch: 0 | 1;
  shuffleRound: number;
  pickedPhotoIndex: Record<string, number>;
  moments: string[];
  rhythm: SpreadRhythmId | null;
  commute: RequestedCommuteMode | null;
  avoidIds: MobileAvoidId[];
  cityName: string | null;
  days: number | null;
  dateMode: "undecided" | "exact";
  startDate: string | null;
  companion: MobileCompanionRelation | null;
  adults: number;
  children: number;
  mustInclude: MobileMustIncludePlace[];
  accommodation: string;
  fromCity: string;
  rawNotes: string;
  ownerUserId: string | null;
  updatedAt: number;
}

export interface SpreadStartSubmitIntent {
  draftRevision: number;
  createdAt: number;
}

export interface SpreadStartCityPhoto {
  fullUrl: string;
  smallUrl: string;
  credit: string;
}

/**
 * 原型两批顺序（每批 6 张）；文案与分类映射严格取自手机端 MOBILE_SCENES。
 * 照片全部来自 R2 图库（scene-opt/ 由 scene-photo-manifest.json 生成；city-opt/ 来自城市图库），
 * 顺序与原型 app.js 的 SCENES 一致。
 */
const SCENE_DISPLAY_ORDER = [
  "street_1",
  "nature_1",
  "food_1",
  "night_1",
  "street_2",
  "nature_2",
  "food_2",
  "night_2",
  "street_3",
  "nature_3",
  "food_3",
  "night_3",
] as const;

const SCENE_CITY_R2_PATHS: Partial<Record<string, readonly string[]>> = {
  street_1: [
    "/city-opt/shanghai/nuno-alberto-MykFFC5zolE-unsplash.webp",
    "/city-opt/chengdu/declan-sun-jmJLdFpntps-unsplash.webp",
  ],
  nature_1: ["/city-opt/nanjing/jennifer-chen-Pnc2Uxb7PG0-unsplash.webp"],
  night_1: ["/city-opt/chongqing/albert-canite-RG2YD21o81E-unsplash.webp"],
  street_2: ["/city-opt/xiamen/commons-130793291.webp"],
  nature_2: ["/city-opt/chengdu/bamboo-joe-EoVw0eEM4lQ-unsplash.webp"],
  nature_3: ["/city-opt/hangzhou/zhu-edward-peq-khnWDbg-unsplash.webp"],
  night_3: ["/city-opt/shanghai/zhou-xian-7tFFO6Mq5L4-unsplash.webp"],
};

const BUNDLED_SCENE_PHOTOS: Record<string, readonly SpreadStartScenePhoto[]> = {
  street_1: [
    { fullUrl: CONCEPT_ASSETS.alley, smallUrl: CONCEPT_ASSETS.alley, credit: "云途 · 老街巷弄" },
    { fullUrl: CONCEPT_ASSETS.rooftops, smallUrl: CONCEPT_ASSETS.rooftops, credit: "云途 · 山城瓦顶" },
  ],
  nature_1: [
    { fullUrl: CONCEPT_ASSETS.lake, smallUrl: CONCEPT_ASSETS.lake, credit: "云途 · 湖畔微风" },
    { fullUrl: CONCEPT_ASSETS.forest, smallUrl: CONCEPT_ASSETS.forest, credit: "云途 · 林间栈道" },
  ],
  food_1: [
    { fullUrl: CONCEPT_ASSETS.food, smallUrl: CONCEPT_ASSETS.food, credit: "云途 · 市井食摊" },
    { fullUrl: CONCEPT_ASSETS.cafe, smallUrl: CONCEPT_ASSETS.cafe, credit: "云途 · 转角咖啡" },
  ],
  night_1: [
    { fullUrl: CONCEPT_ASSETS.night, smallUrl: CONCEPT_ASSETS.night, credit: "云途 · 华灯江夜" },
    { fullUrl: BUNDLED_FALLBACK_IMAGE, smallUrl: BUNDLED_FALLBACK_IMAGE, credit: "重庆 · 山城夜景" },
  ],
  street_2: [
    { fullUrl: CONCEPT_ASSETS.rooftops, smallUrl: CONCEPT_ASSETS.rooftops, credit: "云途 · 坡道街景" },
    { fullUrl: CONCEPT_ASSETS.alley, smallUrl: CONCEPT_ASSETS.alley, credit: "云途 · 城市慢走" },
  ],
  nature_2: [
    { fullUrl: CONCEPT_ASSETS.forest, smallUrl: CONCEPT_ASSETS.forest, credit: "云途 · 绿意林荫" },
    { fullUrl: CONCEPT_ASSETS.coast, smallUrl: CONCEPT_ASSETS.coast, credit: "云途 · 水岸日暮" },
  ],
  food_2: [
    { fullUrl: CONCEPT_ASSETS.cafe, smallUrl: CONCEPT_ASSETS.cafe, credit: "云途 · 午后茶歇" },
    { fullUrl: CONCEPT_ASSETS.food, smallUrl: CONCEPT_ASSETS.food, credit: "云途 · 在地风味" },
  ],
  night_2: [
    { fullUrl: BUNDLED_FALLBACK_IMAGE, smallUrl: BUNDLED_FALLBACK_IMAGE, credit: "重庆 · 两江灯火" },
    { fullUrl: CONCEPT_ASSETS.night, smallUrl: CONCEPT_ASSETS.night, credit: "云途 · 夜市灯光" },
  ],
  street_3: [
    { fullUrl: CONCEPT_ASSETS.alley, smallUrl: CONCEPT_ASSETS.alley, credit: "云途 · 建筑肌理" },
    { fullUrl: CONCEPT_ASSETS.cafe, smallUrl: CONCEPT_ASSETS.cafe, credit: "云途 · 文艺橱窗" },
  ],
  nature_3: [
    { fullUrl: CONCEPT_ASSETS.coast, smallUrl: CONCEPT_ASSETS.coast, credit: "云途 · 远山海风" },
    { fullUrl: CONCEPT_ASSETS.lake, smallUrl: CONCEPT_ASSETS.lake, credit: "云途 · 清晨薄雾" },
  ],
  food_3: [
    { fullUrl: CONCEPT_ASSETS.food, smallUrl: CONCEPT_ASSETS.food, credit: "云途 · 夜市烟火" },
    { fullUrl: CONCEPT_ASSETS.cafe, smallUrl: CONCEPT_ASSETS.cafe, credit: "云途 · 小馆日常" },
  ],
  night_3: [
    { fullUrl: CONCEPT_ASSETS.night, smallUrl: CONCEPT_ASSETS.night, credit: "云途 · 夜色归途" },
    { fullUrl: CONCEPT_ASSETS.rooftops, smallUrl: CONCEPT_ASSETS.rooftops, credit: "云途 · 暮色天台" },
  ],
};

function withConfiguredSceneBase(url: string): string {
  if (!url.startsWith("/scene-opt/")) return url;
  const rawBase = (import.meta.env.VITE_CITY_ASSET_BASE_URL ?? "")
    .trim()
    .replace(/\/+$/, "");
  return rawBase ? `${rawBase}${url}` : url;
}

const MOBILE_SCENE_BY_ID = new Map(MOBILE_SCENES.map((s) => [s.id, s]));

export const SPREAD_START_SCENES: readonly SpreadStartScene[] =
  SCENE_DISPLAY_ORDER.map((id, idx) => {
    const mobileScene = MOBILE_SCENE_BY_ID.get(id);
    if (!mobileScene) {
      throw new Error(`Missing scene definition in MOBILE_SCENES: ${id}`);
    }

    const photos: SpreadStartScenePhoto[] = usesBundledCityAssets()
      ? [...(BUNDLED_SCENE_PHOTOS[id] ?? [])]
      : [
          ...(SCENE_PHOTOS_BY_ID[id] ?? []).map((rec) => ({
            fullUrl: withConfiguredSceneBase(rec.desktopUrl),
            smallUrl: withConfiguredSceneBase(rec.mobileUrl),
            credit: rec.credit,
          })),
          ...(SCENE_CITY_R2_PATHS[id] ?? []).map((path) => {
            const fullUrl = withCityAssetBase(path);
            return {
              fullUrl,
              smallUrl: getCityMobilePhotoUrl(fullUrl),
              credit: getCityPhotoCredit(fullUrl),
            };
          }),
        ];
    if (photos.length === 0) {
      throw new Error(`No photos configured for scene: ${id}`);
    }

    return {
      id: mobileScene.id,
      category: mobileScene.category,
      title: mobileScene.title,
      short: mobileScene.short,
      tag: mobileScene.tag,
      batch: idx < 6 ? 0 : 1,
      photos,
    };
  });

const CINEMATIC_RHYTHM_BY_VALUE = new Map(
  CINEMATIC_RHYTHM_OPTIONS.map((o) => [o.value, o]),
);

function requireRhythmValue(value: CinematicRhythm): CinematicRhythm {
  return CINEMATIC_RHYTHM_BY_VALUE.get(value)?.value ?? value;
}

export const SPREAD_START_RHYTHMS: readonly SpreadStartRhythmOption[] = [
  {
    id: "slow",
    label: "慢一点",
    desc: "留足空白，喜欢就多待会儿",
    requestPreference: requireRhythmValue("轻松"),
    imageUrl: paceSlowImg,
  },
  {
    id: "moderate",
    label: "刚刚好",
    desc: "有主有次，累了就坐下歇歇",
    requestPreference: requireRhythmValue("适中"),
    imageUrl: paceModerateImg,
  },
  {
    id: "full",
    label: "充实一点",
    desc: "多走几处，把代表风景看全",
    requestPreference: requireRhythmValue("紧凑"),
    imageUrl: paceActiveImg,
  },
];

const COMMUTE_DESC: Record<RequestedCommuteMode, string> = {
  driving: "站与站之间少走路",
  transit: "地铁公交连起来",
  cycling: "近处骑车慢慢逛",
};

const COMMUTE_ICON: Record<RequestedCommuteMode, "car" | "bus" | "bike"> = {
  driving: "car",
  transit: "bus",
  cycling: "bike",
};

export const SPREAD_START_COMMUTES: readonly SpreadStartCommuteOption[] =
  CINEMATIC_COMMUTE_OPTIONS.map((opt) => ({
    id: opt.value,
    label: opt.label,
    desc: COMMUTE_DESC[opt.value],
    icon: COMMUTE_ICON[opt.value],
  }));

export const SPREAD_START_AVOID_ICONS: Record<
  MobileAvoidId,
  "alarm" | "mountain" | "users" | "hourglass"
> = {
  no_early: "alarm",
  less_climb: "mountain",
  less_queue: "users",
  no_rush: "hourglass",
};

export const SPREAD_START_COMPANIONS: readonly SpreadStartCompanionOption[] = [
  {
    id: "solo",
    line: "一个人",
    title: "一个人，自由出走",
    sub: "这几天，只跟着自己的心情",
    icon: "person",
    wide: true,
  },
  {
    id: "partner",
    line: "和另一半",
    title: "和喜欢的人",
    sub: "一起收集新的记忆",
    icon: "heart",
  },
  {
    id: "friends",
    line: "和朋友们",
    title: "和朋友一起",
    sub: "好玩的事，想一起做",
    icon: "people",
  },
  {
    id: "parents",
    line: "陪伴长辈",
    title: "陪父母走走",
    sub: "把相处的时间留长一点",
    icon: "people",
  },
  {
    id: "kids",
    line: "带着孩子",
    title: "带孩子看看",
    sub: "把世界翻开一小页",
    icon: "family",
  },
];

const COMPANION_BY_ID = new Map(
  SPREAD_START_COMPANIONS.map((item) => [item.id, item]),
);
const RHYTHM_BY_ID = new Map(
  SPREAD_START_RHYTHMS.map((item) => [item.id, item]),
);
const SCENE_BY_ID = new Map(
  SPREAD_START_SCENES.map((item) => [item.id, item]),
);
const VALID_AVOID_IDS = new Set<MobileAvoidId>(
  MOBILE_AVOID_OPTIONS.map((item) => item.id),
);

export function createInitialSpreadStartDraft(
  ownerUserId: string | null = null,
): SpreadStartDraft {
  return {
    version: 1,
    revision: 1,
    step: 0,
    batch: 0,
    shuffleRound: 0,
    pickedPhotoIndex: {},
    moments: [],
    rhythm: null,
    commute: null,
    avoidIds: [],
    cityName: null,
    days: null,
    dateMode: "undecided",
    startDate: null,
    companion: null,
    adults: 0,
    children: 0,
    mustInclude: [],
    accommodation: "",
    fromCity: "",
    rawNotes: "",
    ownerUserId,
    updatedAt: Date.now(),
  };
}

export function getSceneById(id: string | undefined): SpreadStartScene | undefined {
  return id ? SCENE_BY_ID.get(id) : undefined;
}

export function getRhythmById(
  id: SpreadRhythmId | null | undefined,
): SpreadStartRhythmOption | undefined {
  return id ? RHYTHM_BY_ID.get(id) : undefined;
}

export function getCompanionById(
  id: MobileCompanionRelation | null | undefined,
): SpreadStartCompanionOption | undefined {
  return id ? COMPANION_BY_ID.get(id) : undefined;
}

export function getScenePhotoOption(
  scene: SpreadStartScene,
  draft: Pick<SpreadStartDraft, "pickedPhotoIndex" | "shuffleRound">,
): SpreadStartScenePhoto {
  const rawIdx =
    scene.id in draft.pickedPhotoIndex
      ? draft.pickedPhotoIndex[scene.id]
      : Math.floor(draft.shuffleRound / 2);
  const safeIdx =
    ((rawIdx % scene.photos.length) + scene.photos.length) % scene.photos.length;
  return scene.photos[safeIdx];
}

export function getSpreadStartBatchScenes(
  batch: number,
  selectedIds: readonly string[],
): SpreadStartScene[] {
  const normalizedBatch = ((batch % 2) + 2) % 2;
  const batchItems = SPREAD_START_SCENES.filter(
    (item) => item.batch === normalizedBatch,
  );
  return batchItems.map((x, idxInBatch) => {
    if (selectedIds.includes(x.id)) {
      return x;
    }
    return (
      SPREAD_START_SCENES.find(
        (candidate, idx) =>
          selectedIds.includes(candidate.id) && idx % 6 === idxInBatch,
      ) || x
    );
  });
}

export function isSpreadStepValid(
  draft: Pick<
    SpreadStartDraft,
    "moments" | "rhythm" | "cityName" | "days" | "companion"
  >,
  stepIndex: number,
): boolean {
  switch (stepIndex) {
    case 0:
      return draft.moments.length > 0;
    case 1:
      return Boolean(draft.rhythm);
    case 2:
      return Boolean(draft.cityName && draft.cityName.trim());
    case 3:
      return Boolean(
        draft.days &&
          draft.days >= 1 &&
          draft.days <= 7 &&
          draft.companion,
      );
    default:
      return false;
  }
}

export function furthestAllowedStep(
  draft: Pick<
    SpreadStartDraft,
    "moments" | "rhythm" | "cityName" | "days" | "companion"
  >,
): 0 | 1 | 2 | 3 {
  let i = 0;
  while (i < SPREAD_START_STEPS.length - 1 && isSpreadStepValid(draft, i)) {
    i += 1;
  }
  return i as 0 | 1 | 2 | 3;
}

export function sanitizeSpreadStartDraft(raw: unknown): SpreadStartDraft | null {
  if (!raw || typeof raw !== "object") return null;
  const d = raw as Record<string, unknown>;
  if (d.version !== 1) return null;

  const moments = Array.isArray(d.moments)
    ? Array.from(
        new Set(
          d.moments.filter(
            (id): id is string => typeof id === "string" && SCENE_BY_ID.has(id),
          ),
        ),
      ).slice(0, 4)
    : [];

  const shuffleRound =
    typeof d.shuffleRound === "number" &&
    Number.isInteger(d.shuffleRound) &&
    d.shuffleRound >= 0
      ? d.shuffleRound
      : 0;
  const batch: 0 | 1 = d.batch === 1 ? 1 : 0;

  const pickedPhotoIndex: Record<string, number> = {};
  if (d.pickedPhotoIndex && typeof d.pickedPhotoIndex === "object") {
    for (const [k, v] of Object.entries(d.pickedPhotoIndex as Record<string, unknown>)) {
      if (
        moments.includes(k) &&
        typeof v === "number" &&
        Number.isInteger(v) &&
        v >= 0
      ) {
        pickedPhotoIndex[k] = v;
      }
    }
  }

  const rhythm: SpreadRhythmId | null =
    d.rhythm === "slow" || d.rhythm === "moderate" || d.rhythm === "full"
      ? d.rhythm
      : null;

  const commute: RequestedCommuteMode | null =
    d.commute === "driving" ||
    d.commute === "transit" ||
    d.commute === "cycling"
      ? d.commute
      : null;

  const avoidIds = Array.isArray(d.avoidIds)
    ? Array.from(
        new Set(
          d.avoidIds.filter(
            (id): id is MobileAvoidId =>
              typeof id === "string" && VALID_AVOID_IDS.has(id as MobileAvoidId),
          ),
        ),
      )
    : [];

  const cityName =
    typeof d.cityName === "string" && d.cityName.trim()
      ? cleanTripCityName(d.cityName)
      : null;

  const days =
    typeof d.days === "number" &&
    Number.isInteger(d.days) &&
    d.days >= 1 &&
    d.days <= 7
      ? d.days
      : null;

  let startDate: string | null = null;
  let dateMode: "undecided" | "exact" = "undecided";
  if (
    d.dateMode === "exact" &&
    typeof d.startDate === "string" &&
    isValidCalendarDate(d.startDate) &&
    !isDateInPast(d.startDate)
  ) {
    dateMode = "exact";
    startDate = d.startDate;
  }

  const companion: MobileCompanionRelation | null =
    typeof d.companion === "string" &&
    d.companion in COMPANION_BOUNDARIES
      ? (d.companion as MobileCompanionRelation)
      : null;

  let adults = 0;
  let children = 0;
  if (companion) {
    const rawAdults =
      typeof d.adults === "number" && Number.isInteger(d.adults)
        ? d.adults
        : COMPANION_BOUNDARIES[companion].defaultAdults;
    const rawChildren =
      typeof d.children === "number" && Number.isInteger(d.children)
        ? d.children
        : COMPANION_BOUNDARIES[companion].defaultChildren;
    const clamped = clampCompanionCounts(companion, rawAdults, rawChildren);
    adults = clamped.adults;
    children = clamped.children;
  }

  const mustInclude: MobileMustIncludePlace[] = [];
  if (cityName && Array.isArray(d.mustInclude)) {
    for (const item of d.mustInclude) {
      if (
        item &&
        typeof item === "object" &&
        typeof (item as MobileMustIncludePlace).placeId === "number" &&
        Number.isInteger((item as MobileMustIncludePlace).placeId) &&
        typeof (item as MobileMustIncludePlace).name === "string" &&
        (item as MobileMustIncludePlace).name.trim()
      ) {
        if (mustInclude.length < MOBILE_MUST_INCLUDE_LIMIT) {
          mustInclude.push({
            placeId: (item as MobileMustIncludePlace).placeId,
            name: (item as MobileMustIncludePlace).name.trim(),
          });
        }
      }
    }
  }

  const accommodation =
    cityName && typeof d.accommodation === "string"
      ? d.accommodation.slice(0, SPREAD_START_ACCOMMODATION_MAX)
      : "";
  const fromCity =
    typeof d.fromCity === "string"
      ? d.fromCity.slice(0, SPREAD_START_FROM_CITY_MAX)
      : "";
  const rawNotes =
    typeof d.rawNotes === "string"
      ? d.rawNotes.slice(0, SPREAD_START_NOTES_MAX)
      : "";

  const ownerUserId =
    typeof d.ownerUserId === "string" && d.ownerUserId ? d.ownerUserId : null;
  const revision =
    typeof d.revision === "number" &&
    Number.isInteger(d.revision) &&
    d.revision >= 1
      ? d.revision
      : 1;

  const baseDraft: SpreadStartDraft = {
    version: 1,
    revision,
    step: 0,
    batch,
    shuffleRound,
    pickedPhotoIndex,
    moments,
    rhythm,
    commute,
    avoidIds,
    cityName,
    days,
    dateMode,
    startDate,
    companion,
    adults,
    children,
    mustInclude,
    accommodation,
    fromCity,
    rawNotes,
    ownerUserId,
    updatedAt:
      typeof d.updatedAt === "number" && Number.isFinite(d.updatedAt)
        ? d.updatedAt
        : Date.now(),
  };

  const maxAllowed = furthestAllowedStep(baseDraft);
  const rawStep =
    typeof d.step === "number" &&
    Number.isInteger(d.step) &&
    d.step >= 0 &&
    d.step <= 3
      ? (d.step as 0 | 1 | 2 | 3)
      : 0;
  baseDraft.step = (rawStep <= maxAllowed ? rawStep : maxAllowed) as
    | 0
    | 1
    | 2
    | 3;

  return baseDraft;
}

export function loadSpreadStartDraft(): SpreadStartDraft | null {
  try {
    const raw = sessionStorage.getItem(SPREAD_START_DRAFT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    return sanitizeSpreadStartDraft(parsed);
  } catch {
    return null;
  }
}

export function saveSpreadStartDraft(draft: SpreadStartDraft): void {
  try {
    sessionStorage.setItem(SPREAD_START_DRAFT_KEY, JSON.stringify(draft));
  } catch {
    /* ignore storage quota/privacy errors */
  }
}

export function clearSpreadStartDraft(): void {
  try {
    sessionStorage.removeItem(SPREAD_START_DRAFT_KEY);
  } catch {
    /* ignore */
  }
}

export function saveSpreadStartSubmitIntent(draftRevision: number): void {
  try {
    const payload: SpreadStartSubmitIntent = {
      draftRevision,
      createdAt: Date.now(),
    };
    sessionStorage.setItem(
      SPREAD_START_SUBMIT_INTENT_KEY,
      JSON.stringify(payload),
    );
  } catch {
    /* ignore */
  }
}

export function peekSpreadStartSubmitIntent(): SpreadStartSubmitIntent | null {
  try {
    const raw = sessionStorage.getItem(SPREAD_START_SUBMIT_INTENT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<SpreadStartSubmitIntent>;
    if (
      !parsed ||
      typeof parsed.draftRevision !== "number" ||
      !Number.isInteger(parsed.draftRevision)
    ) {
      return null;
    }
    return {
      draftRevision: parsed.draftRevision,
      createdAt:
        typeof parsed.createdAt === "number" ? parsed.createdAt : Date.now(),
    };
  } catch {
    return null;
  }
}

export function clearSpreadStartSubmitIntent(): void {
  try {
    sessionStorage.removeItem(SPREAD_START_SUBMIT_INTENT_KEY);
  } catch {
    /* ignore */
  }
}

export function consumeSpreadStartSubmitIntent(
  draftRevision: number,
): boolean {
  const intent = peekSpreadStartSubmitIntent();
  clearSpreadStartSubmitIntent();
  if (!intent) return false;
  return intent.draftRevision === draftRevision;
}

/**
 * 账号绑定/切换/登出清洗：
 * - 匿名期的本地草稿被当前登录用户认领时，保留全部选择与 revision；
 * - 若已有明确 owner 且切换为其他用户或退出登录，清空私人字段与提交意图并递增 revision。
 */
export function scrubSpreadStartDraftOnAuthChange(
  draft: SpreadStartDraft,
  nextUserId: string | null,
): SpreadStartDraft {
  if (draft.ownerUserId === nextUserId) {
    return draft;
  }

  // 匿名草稿在登录后被认领：保持内容和 revision，使一次性待提交意图得以匹配
  if (draft.ownerUserId === null && nextUserId !== null) {
    return {
      ...draft,
      ownerUserId: nextUserId,
      updatedAt: Date.now(),
    };
  }

  // 从账号 A 切到账号 B 或退出登录：清理敏感与强绑定的选择，禁止复用待提交意图
  clearSpreadStartSubmitIntent();
  const scrubbed: SpreadStartDraft = {
    ...draft,
    revision: draft.revision + 1,
    companion: null,
    adults: 0,
    children: 0,
    mustInclude: [],
    accommodation: "",
    fromCity: "",
    rawNotes: "",
    ownerUserId: nextUserId,
    updatedAt: Date.now(),
  };
  const maxStep = furthestAllowedStep(scrubbed);
  if (scrubbed.step > maxStep) {
    scrubbed.step = maxStep;
  }
  return scrubbed;
}

export function getLocalTodayIso(now: Date = new Date()): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export function computeTargetCalendarOffset(
  startDate: string | null,
  dateMode: "undecided" | "exact",
  now: Date = new Date(),
): number {
  if (dateMode !== "exact" || !startDate || !isValidCalendarDate(startDate)) {
    return 0;
  }
  const [sy, sm] = startDate.split("-").map((n) => parseInt(n, 10));
  if (!sy || !sm) return 0;
  const diff = (sy - now.getFullYear()) * 12 + (sm - (now.getMonth() + 1));
  return Math.min(11, Math.max(0, diff));
}

export function formatCnMonthDay(iso: string): string {
  if (!isValidCalendarDate(iso)) return "";
  const [, m, d] = iso.split("-").map((n) => parseInt(n, 10));
  return m && d ? `${m}月${d}日` : "";
}

const CN_WEEK = "日一二三四五六";

export function formatTicketDateParts(iso: string): {
  md: string;
  week: string;
} {
  if (!isValidCalendarDate(iso)) {
    return { md: "", week: "" };
  }
  const [y, m, d] = iso.split("-").map((n) => parseInt(n, 10));
  const wd = new Date(y, m - 1, d).getDay();
  return {
    md: `${m}月${d}日`,
    week: `周${CN_WEEK[wd] ?? ""}`,
  };
}

export function resolveSpreadStartCityPhoto(
  cityName: string,
  destinations: readonly Destination[],
): SpreadStartCityPhoto | null {
  const cleanCity = cleanTripCityName(cityName);
  if (!cleanCity) return null;

  if (CITY_NAME_TO_FOLDER[cleanCity]) {
    const items = getCityGalleryItems(cleanCity);
    if (items.length === 0) return null;
    const first = items[0];
    return {
      fullUrl: first.url,
      smallUrl: getCityMobilePhotoUrl(first.url),
      credit: first.credit,
    };
  }

  const matched = destinations.find(
    (d) => cleanTripCityName(d.name) === cleanCity,
  );
  const picked = pickNonGalleryDestinationPhoto(cleanCity, matched);
  if (!picked) return null;
  return {
    fullUrl: picked.url,
    smallUrl: getCityMobilePhotoUrl(picked.url),
    credit: picked.credit,
  };
}

export function getCityTicketCode(
  cityName: string,
  destination?: Destination,
): string {
  const cleanCity = cleanTripCityName(cityName);
  const folder = cleanCity ? CITY_NAME_TO_FOLDER[cleanCity] : undefined;
  return (folder || destination?.nameEn || "").trim().toUpperCase();
}

export function buildSpreadStartRequest(
  draft: SpreadStartDraft,
): TripSubmitRequest {
  const companion = draft.companion ?? "solo";
  const clamped = clampCompanionCounts(companion, draft.adults, draft.children);
  const peopleCount = clamped.adults + clamped.children;

  // 1. 心动画面偏好词 + 节奏偏好词
  const preferenceSet = new Set<string>();
  for (const sceneId of draft.moments) {
    const scene = SCENE_BY_ID.get(sceneId);
    if (scene) {
      const keyword = SCENE_CATEGORY_INTEREST_MAP[scene.category];
      if (keyword) preferenceSet.add(keyword);
    }
  }

  const rhythmOpt = getRhythmById(draft.rhythm);
  if (rhythmOpt) {
    preferenceSet.add(rhythmOpt.requestPreference);
  }

  // 2. 避开选项（仅选中的放入）
  const avoidList: string[] = [];
  for (const avoidId of draft.avoidIds) {
    const opt = MOBILE_AVOID_OPTIONS.find((item) => item.id === avoidId);
    if (opt) avoidList.push(opt.requestText);
  }

  // 3. 备注与同行情况（与 buildMobileTripRequest 完全一致，总长不超过 2000）
  const companionBlock = formatCompanionNote(
    companion,
    clamped.adults,
    clamped.children,
  );
  let userNote = (draft.rawNotes || "").trim();
  if (userNote.length > SPREAD_START_NOTES_MAX) {
    userNote = userNote.slice(0, SPREAD_START_NOTES_MAX);
  }

  let finalNotes: string;
  if (userNote) {
    const combined = `${userNote}\n${companionBlock}`;
    if (combined.length > 2000) {
      const maxUserNoteLen = Math.max(0, 2000 - companionBlock.length - 1);
      finalNotes =
        maxUserNoteLen > 0
          ? `${userNote.slice(0, maxUserNoteLen)}\n${companionBlock}`
          : companionBlock;
    } else {
      finalNotes = combined;
    }
  } else {
    finalNotes = companionBlock;
  }

  const validDays =
    typeof draft.days === "number" &&
    Number.isInteger(draft.days) &&
    draft.days >= 1 &&
    draft.days <= 7
      ? draft.days
      : 1;

  const request: TripSubmitRequest = {
    to_city: cleanTripCityName(draft.cityName || ""),
    days: validDays,
    people_count: peopleCount,
    preferences: Array.from(preferenceSet),
    notes: finalNotes,
  };

  if (avoidList.length > 0) {
    request.avoid = avoidList;
  }

  // 4. 在城里怎么走：打车不传，仅公共交通 / 骑行传递
  if (draft.commute === "transit" || draft.commute === "cycling") {
    request.commute_mode = draft.commute;
  }

  // 5. 日期：选填，未定不传 start_date / end_date
  if (
    draft.dateMode === "exact" &&
    draft.startDate &&
    isValidCalendarDate(draft.startDate) &&
    !isDateInPast(draft.startDate)
  ) {
    request.start_date = draft.startDate;
    request.end_date = computeEndDate(draft.startDate, validDays);
  }

  // 6. 还有要交代的：填了才传
  const cleanFromCity = (draft.fromCity || "").trim();
  if (cleanFromCity) {
    request.from_city = cleanFromCity;
  }

  const cleanAccommodation = (draft.accommodation || "").trim();
  if (cleanAccommodation) {
    request.accommodation = { name: cleanAccommodation };
  }

  if (draft.mustInclude.length > 0) {
    request.must_include = draft.mustInclude
      .slice(0, MOBILE_MUST_INCLUDE_LIMIT)
      .map((item) => ({ place_id: item.placeId, name: item.name }));
  }

  return request;
}
