import type {
  MobileAvoidId,
  MobileCompanionRelation,
  MobileMustIncludePlace,
  MobileRhythm,
  MobileScene,
  MobileStep,
  MobileTripDraft,
} from "@/types/mobile";
import {
  COMPANION_BOUNDARIES,
  MOBILE_AVOID_OPTIONS,
  MOBILE_MUST_INCLUDE_LIMIT,
  MOBILE_SCENES,
  SCENE_CATEGORY_INTEREST_MAP,
} from "@/types/mobile";
import type { TripSubmitRequest } from "@/types/tripRequest";

export const MOBILE_DRAFT_KEY = "yuntu-mobile-draft-v1";

const STEP_ORDER: Record<MobileStep, number> = {
  home: 0,
  moments: 1,
  rhythm: 2,
  profile: 3,
  departure: 4,
  companions: 5,
  review: 6,
};

/**
 * 创建空白初始草稿
 */
export function createInitialMobileDraft(
  ownerUserId: string | null = null,
): MobileTripDraft {
  const defaultCompanion: MobileCompanionRelation = "solo";
  const bounds = COMPANION_BOUNDARIES[defaultCompanion];

  return {
    version: 1,
    revision: 1,
    step: "moments",
    entry: "quiz",
    mustInclude: [],
    sceneIds: [],
    momentBatch: 0,
    seenSceneIds: [],
    rhythm: null,
    cityId: null,
    cityName: null,
    cityConfirmed: false,
    days: 3,
    dateMode: "undecided",
    startDate: null,
    companion: defaultCompanion,
    adults: bounds.defaultAdults,
    children: bounds.defaultChildren,
    avoidIds: [],
    rawNotes: "",
    editReturn: null,
    ownerUserId,
    updatedAt: Date.now(),
  };
}

/**
 * 严格按照同行关系约束人数上下限及总人数限制（确保整数）
 */
export function clampCompanionCounts(
  companion: MobileCompanionRelation,
  adults: number,
  children: number,
): { adults: number; children: number } {
  const b = COMPANION_BOUNDARIES[companion] || COMPANION_BOUNDARIES.solo;

  const validAdults =
    typeof adults === "number" && Number.isInteger(adults)
      ? adults
      : Math.floor(adults || 1);
  const validChildren =
    typeof children === "number" && Number.isInteger(children)
      ? children
      : Math.floor(children || 0);

  let clampedAdults = Math.min(b.maxAdults, Math.max(b.minAdults, validAdults));
  let clampedChildren = Math.min(b.maxChildren, Math.max(b.minChildren, validChildren));

  if (clampedAdults + clampedChildren > b.maxTotal) {
    if (b.minChildren > 0) {
      clampedChildren = Math.min(clampedChildren, b.maxTotal - clampedAdults);
      if (clampedChildren < b.minChildren) {
        clampedChildren = b.minChildren;
        clampedAdults = b.maxTotal - clampedChildren;
      }
    } else {
      clampedChildren = Math.max(0, b.maxTotal - clampedAdults);
    }
  }

  return { adults: clampedAdults, children: clampedChildren };
}

/**
 * 格式化同行人说明块，例如：【同行情况】独自一人，1位成人、0位儿童。
 */
export function formatCompanionNote(
  companion: MobileCompanionRelation,
  adults: number,
  children: number,
): string {
  const label = COMPANION_BOUNDARIES[companion]?.label || "同行";
  return `【同行情况】${label}，${adults}位成人、${children}位儿童。`;
}

/**
 * 严格校验 YYYY-MM-DD 格式且具有日历回读一致性（拒绝 2027-02-31 等无效日期）
 */
export function isValidCalendarDate(dateStr: unknown): dateStr is string {
  if (typeof dateStr !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
    return false;
  }
  const parts = dateStr.split("-").map((v) => parseInt(v, 10));
  if (parts.length !== 3 || parts.some(isNaN)) return false;
  const [year, month, day] = parts;
  if (year < 1900 || year > 2100 || month < 1 || month > 12 || day < 1 || day > 31) {
    return false;
  }
  const d = new Date(year, month - 1, day);
  return (
    d.getFullYear() === year &&
    d.getMonth() === month - 1 &&
    d.getDate() === day
  );
}

/**
 * 本地时区计算结束日期（本地日历推算，避免 UTC 跨日偏差）
 * 包含首尾天数：end_date = start_date + days - 1
 */
export function computeEndDate(startDate: string, days: number): string {
  if (!isValidCalendarDate(startDate)) return startDate;
  const parts = startDate.split("-").map((v) => parseInt(v, 10));
  const [year, month, day] = parts;
  const validDays = Math.max(1, Math.floor(days));
  const target = new Date(year, month - 1, day + validDays - 1);
  const y = target.getFullYear();
  const m = String(target.getMonth() + 1).padStart(2, "0");
  const d = String(target.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/**
 * 校验指定 YYYY-MM-DD 日期是否在本地今天之前（过期日期）
 */
export function isDateInPast(dateStr: string): boolean {
  if (!isValidCalendarDate(dateStr)) return true;
  const parts = dateStr.split("-").map((v) => parseInt(v, 10));
  const target = new Date(parts[0], parts[1] - 1, parts[2]);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return target < today;
}

/**
 * 获取当前批次展示的 4 张卡片。
 * 换一批规则：如果某位置卡片已被选中，该位置保持已选卡片固定不变；未选中的位置轮换至当前批次候选。
 */
export function getActiveBatchScenes(
  currentBatch: number,
  selectedIds: string[],
): MobileScene[] {
  const categories: MobileScene["category"][] = [
    "street",
    "nature",
    "food",
    "night",
  ];

  return categories.map((cat) => {
    // 检查此类别下是否有已选中的卡片
    const selectedInCategory = MOBILE_SCENES.find(
      (s) => s.category === cat && selectedIds.includes(s.id),
    );
    if (selectedInCategory) {
      return selectedInCategory;
    }
    // 否则取当前批次该类别的卡片
    const batchScene = MOBILE_SCENES.find(
      (s) => s.category === cat && s.batchIndex === currentBatch % 3,
    );
    return batchScene || MOBILE_SCENES.find((s) => s.category === cat)!;
  });
}

/**
 * 是否允许换一批：已选满 4 张时禁用换一批
 */
export function canRotateBatch(selectedIds: string[]): boolean {
  return selectedIds.length < 4;
}

/**
 * 纯函数构造手机提交请求体 (TripSubmitRequest)
 * 严格遵从产品设计契约：
 * 1. 日期未定：start_date / end_date 完全省略（属性不存在，绝无空串 ""）
 * 2. 日期确切：本地日历推算 end_date
 * 3. 12 场景 4 类：去重映射为 citywalk / 自然风光 / 美食 / 夜景
 * 4. 节奏：追加原意词“慢一点”/“充实一点”
 * 5. 叮嘱：原文“不想早起、尽量少爬坡、尽量少排队、不想赶行程”
 * 6. 同行：people_count = adults + children；并在 notes 末尾追加一次【同行情况】说明块
 * 7. 原始备注最长 300 字，请求总长不超过 BFF 2000 字符限制
 * 8. 原型未提供项（budget、commute_mode、accommodation 等）不发；must_include 只来自「随手收集灵感」的目录地点
 */
export function buildMobileTripRequest(
  draft: MobileTripDraft,
): TripSubmitRequest {
  const clamped = clampCompanionCounts(
    draft.companion,
    draft.adults,
    draft.children,
  );
  const peopleCount = clamped.adults + clamped.children;

  // 1. 场景偏好映射并去重
  const preferenceSet = new Set<string>();
  for (const sceneId of draft.sceneIds) {
    const scene = MOBILE_SCENES.find((s) => s.id === sceneId);
    if (scene) {
      const keyword =
        SCENE_CATEGORY_INTEREST_MAP[scene.category] || scene.interestKeyword;
      if (keyword) preferenceSet.add(keyword);
    }
  }

  // 2. 节奏追加
  if (draft.rhythm === "slow") {
    preferenceSet.add("慢一点");
  } else if (draft.rhythm === "full") {
    preferenceSet.add("充实一点");
  }

  // 3. 叮嘱原文映射
  const avoidList: string[] = [];
  for (const avoidId of draft.avoidIds) {
    const opt = MOBILE_AVOID_OPTIONS.find((o) => o.id === avoidId);
    if (opt) avoidList.push(opt.requestText);
  }

  // 4. 同行备注追加与长度控制（BFF 限制整体 notes <= 2000）
  const companionBlock = formatCompanionNote(
    draft.companion,
    clamped.adults,
    clamped.children,
  );
  let userNote = (draft.rawNotes || "").trim();
  if (userNote.length > 300) {
    userNote = userNote.slice(0, 300);
  }

  let finalNotes: string;
  if (userNote) {
    const combined = `${userNote}\n${companionBlock}`;
    if (combined.length > 2000) {
      const maxUserNoteLen = Math.max(0, 2000 - companionBlock.length - 1);
      finalNotes = `${userNote.slice(0, maxUserNoteLen)}\n${companionBlock}`;
    } else {
      finalNotes = combined;
    }
  } else {
    finalNotes = companionBlock;
  }

  const request: TripSubmitRequest = {
    to_city: draft.cityName || "",
    days: draft.days ?? 3,
    people_count: peopleCount,
    preferences: Array.from(preferenceSet),
    avoid: avoidList,
    notes: finalNotes,
  };

  // 5. 日期模式处理
  if (draft.dateMode === "exact" && draft.startDate && isValidCalendarDate(draft.startDate)) {
    request.start_date = draft.startDate;
    request.end_date = computeEndDate(draft.startDate, draft.days || 1);
  }
  // 注意：若 draft.dateMode === "undecided"，request 上不附加 start_date/end_date 属性

  // 6. 「随手收集灵感」带上的目录地点；没有时不发 must_include
  if (draft.mustInclude.length > 0) {
    request.must_include = draft.mustInclude.map((p) => ({ place_id: p.placeId, name: p.name }));
  }

  return request;
}

/**
 * 根据草稿内容计算首个未完成的必填步骤（断点恢复）
 */
export function resumeMobileDraftStep(draft: MobileTripDraft): MobileStep {
  // 「随手收集灵感」照原型不挑画面、节奏选填，带着地点直接到出发
  if (draft.entry !== "inspiration") {
    if (draft.sceneIds.length === 0) {
      return "moments";
    }
    if (!draft.rhythm) {
      return "rhythm";
    }
  }
  if (!draft.cityName || !draft.cityConfirmed) {
    return "profile";
  }
  if (
    draft.days === null ||
    !Number.isInteger(draft.days) ||
    draft.days < 1 ||
    draft.days > 7 ||
    (draft.dateMode === "exact" &&
      (!draft.startDate ||
        !isValidCalendarDate(draft.startDate) ||
        isDateInPast(draft.startDate)))
  ) {
    return "departure";
  }
  const bounds = COMPANION_BOUNDARIES[draft.companion];
  if (
    !bounds ||
    typeof draft.adults !== "number" ||
    !Number.isInteger(draft.adults) ||
    draft.adults < bounds.minAdults ||
    draft.adults > bounds.maxAdults ||
    typeof draft.children !== "number" ||
    !Number.isInteger(draft.children) ||
    draft.children < bounds.minChildren ||
    draft.children > bounds.maxChildren ||
    draft.adults + draft.children > bounds.maxTotal
  ) {
    return "companions";
  }
  return "review";
}

/**
 * 校验并清理从外部或存储恢复的草稿数据
 */
export function sanitizeMobileDraft(raw: unknown): MobileTripDraft | null {
  if (!raw || typeof raw !== "object") return null;
  const d = raw as Record<string, unknown>;

  if (d.version !== 1) return null;

  const validSteps: MobileStep[] = [
    "home",
    "moments",
    "rhythm",
    "profile",
    "departure",
    "companions",
    "review",
  ];
  const step: MobileStep = validSteps.includes(d.step as MobileStep)
    ? (d.step as MobileStep)
    : "moments";

  const allSceneIds = new Set(MOBILE_SCENES.map((s) => s.id));
  const rawSceneIds = Array.isArray(d.sceneIds) ? d.sceneIds : [];
  const sceneIds = rawSceneIds
    .filter((id): id is string => typeof id === "string" && allSceneIds.has(id))
    .slice(0, 4);

  const momentBatch =
    typeof d.momentBatch === "number" &&
    Number.isInteger(d.momentBatch) &&
    d.momentBatch >= 0
      ? d.momentBatch % 3
      : 0;

  const rhythm: MobileRhythm | null =
    d.rhythm === "slow" || d.rhythm === "full" ? d.rhythm : null;

  const cityId = typeof d.cityId === "string" ? d.cityId : null;
  const cityName = typeof d.cityName === "string" ? d.cityName : null;
  const cityConfirmed = Boolean(d.cityConfirmed && cityName);

  // 严格要求有限正整数 1-7
  let days: number | null = null;
  if (
    typeof d.days === "number" &&
    Number.isInteger(d.days) &&
    d.days >= 1 &&
    d.days <= 7
  ) {
    days = d.days;
  }

  const dateMode: "undecided" | "exact" =
    d.dateMode === "exact" ? "exact" : "undecided";

  // 严格日历回读一致性校验（拒绝 2027-02-31 等非法日期）
  let startDate: string | null = null;
  if (
    typeof d.startDate === "string" &&
    isValidCalendarDate(d.startDate)
  ) {
    startDate = d.startDate;
  }

  const validCompanions: MobileCompanionRelation[] = [
    "solo",
    "partner",
    "friends",
    "parents",
    "kids",
  ];
  const companion: MobileCompanionRelation = validCompanions.includes(
    d.companion as MobileCompanionRelation,
  )
    ? (d.companion as MobileCompanionRelation)
    : "solo";

  const rawAdults =
    typeof d.adults === "number" && Number.isInteger(d.adults) ? d.adults : 1;
  const rawChildren =
    typeof d.children === "number" && Number.isInteger(d.children)
      ? d.children
      : 0;
  const { adults, children } = clampCompanionCounts(
    companion,
    rawAdults,
    rawChildren,
  );

  const validAvoidIds = new Set(MOBILE_AVOID_OPTIONS.map((o) => o.id));
  const rawAvoidIds = Array.isArray(d.avoidIds) ? d.avoidIds : [];
  const avoidIds = rawAvoidIds.filter((id): id is MobileAvoidId =>
    typeof id === "string" && validAvoidIds.has(id as MobileAvoidId),
  );

  let rawNotes = typeof d.rawNotes === "string" ? d.rawNotes : "";
  if (rawNotes.length > 300) rawNotes = rawNotes.slice(0, 300);

  const editReturn = validSteps.includes(d.editReturn as MobileStep)
    ? (d.editReturn as MobileStep)
    : null;

  const ownerUserId = typeof d.ownerUserId === "string" ? d.ownerUserId : null;
  const revision =
    typeof d.revision === "number" && Number.isInteger(d.revision) && d.revision >= 1
      ? d.revision
      : 1;
  const updatedAt = typeof d.updatedAt === "number" ? d.updatedAt : Date.now();

  const entry: MobileTripDraft["entry"] =
    d.entry === "direct" || d.entry === "inspiration" ? d.entry : "quiz";
  const seenPlaceIds = new Set<number>();
  const mustInclude = (Array.isArray(d.mustInclude) ? d.mustInclude : [])
    .filter((item): item is MobileMustIncludePlace => {
      if (!item || typeof item !== "object") return false;
      const { placeId, name } = item as Record<string, unknown>;
      if (typeof placeId !== "number" || !Number.isInteger(placeId) || placeId < 1) return false;
      if (typeof name !== "string" || !name.trim() || seenPlaceIds.has(placeId)) return false;
      seenPlaceIds.add(placeId);
      return true;
    })
    .slice(0, MOBILE_MUST_INCLUDE_LIMIT)
    .map(({ placeId, name }) => ({ placeId, name }));

  const candidateDraft: MobileTripDraft = {
    version: 1,
    revision,
    step,
    entry,
    // 地点只属于所选城市；城市没确认时一并丢弃
    mustInclude: cityConfirmed && cityName ? mustInclude : [],
    sceneIds,
    momentBatch,
    seenSceneIds: [],
    rhythm,
    cityId,
    cityName,
    cityConfirmed,
    days,
    dateMode,
    startDate,
    companion,
    adults,
    children,
    avoidIds,
    rawNotes,
    editReturn,
    ownerUserId,
    updatedAt,
  };

  // 无效输入回正确步骤：如果传入的 step 超过了数据实际满足的最高步骤，回退到首个未完成步骤
  const earliestRequiredStep = resumeMobileDraftStep(candidateDraft);
  if (STEP_ORDER[step] > STEP_ORDER[earliestRequiredStep]) {
    candidateDraft.step = earliestRequiredStep;
  }

  return candidateDraft;
}

/**
 * 账号切换与认领处理：
 * - 匿名草稿由刚登录的账号认领（ownerUserId: null -> newUserId）；
 * - 主动换号或退出时，清除敏感内容（rawNotes、同伴人数恢复默认），防止泄漏给新账号；
 * - 初始认证初始化（unknown/bootstrap）不视为退出，不抹除草稿。
 */
export function handleAccountSwitchForMobileDraft(
  currentDraft: MobileTripDraft | null,
  newUserId: string | null,
): MobileTripDraft | null {
  if (!currentDraft) return null;

  if (currentDraft.ownerUserId === null && newUserId) {
    // 认领匿名草稿（不修改草稿实际内容，仅绑定归属，保持 revision 一致以便恢复待提交意图）
    return {
      ...currentDraft,
      ownerUserId: newUserId,
      updatedAt: Date.now(),
    };
  }

  if (
    currentDraft.ownerUserId &&
    newUserId &&
    currentDraft.ownerUserId !== newUserId
  ) {
    // 换号：擦除敏感备注与自定义同伴设置
    const bounds = COMPANION_BOUNDARIES.solo;
    return {
      ...currentDraft,
      ownerUserId: newUserId,
      rawNotes: "",
      companion: "solo",
      adults: bounds.defaultAdults,
      children: bounds.defaultChildren,
      revision: currentDraft.revision + 1,
      updatedAt: Date.now(),
    };
  }

  if (currentDraft.ownerUserId && !newUserId) {
    // 主动登出：清除敏感备注，恢复为匿名草稿
    const bounds = COMPANION_BOUNDARIES.solo;
    return {
      ...currentDraft,
      ownerUserId: null,
      rawNotes: "",
      companion: "solo",
      adults: bounds.defaultAdults,
      children: bounds.defaultChildren,
      revision: currentDraft.revision + 1,
      updatedAt: Date.now(),
    };
  }

  return currentDraft;
}

export function saveMobileDraft(draft: MobileTripDraft): void {
  try {
    sessionStorage.setItem(MOBILE_DRAFT_KEY, JSON.stringify(draft));
  } catch {
    /* 忽略 sessionStorage 配额超限或无痕模式异常，保持内存中工作 */
  }
}

export function loadMobileDraft(): MobileTripDraft | null {
  try {
    const raw = sessionStorage.getItem(MOBILE_DRAFT_KEY);
    if (!raw) return null;
    return sanitizeMobileDraft(JSON.parse(raw));
  } catch {
    return null;
  }
}

export function clearMobileDraft(): void {
  try {
    sessionStorage.removeItem(MOBILE_DRAFT_KEY);
  } catch {
    /* ignore */
  }
}

export const MOBILE_EDIT_SNAPSHOT_KEY = "yuntu-mobile-edit-snapshot-v1";

export function saveMobileEditSnapshot(snapshot: MobileTripDraft): void {
  try {
    sessionStorage.setItem(MOBILE_EDIT_SNAPSHOT_KEY, JSON.stringify(snapshot));
  } catch {
    /* ignore storage errors */
  }
}

export function loadMobileEditSnapshot(): MobileTripDraft | null {
  try {
    const raw = sessionStorage.getItem(MOBILE_EDIT_SNAPSHOT_KEY);
    if (!raw) return null;
    return sanitizeMobileDraft(JSON.parse(raw));
  } catch {
    return null;
  }
}

export function clearMobileEditSnapshot(): void {
  try {
    sessionStorage.removeItem(MOBILE_EDIT_SNAPSHOT_KEY);
  } catch {
    /* ignore */
  }
}


