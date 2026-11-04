import type { MobileSubmission } from "@/types/mobile";
import type { TripSubmitRequest } from "@/types/tripRequest";
import { generateFingerprint } from "./pendingSubmission";
import { isValidCalendarDate } from "./mobileDraft";

export { generateFingerprint };

export const MOBILE_SUBMISSION_KEY = "yuntu-mobile-submission-v1";

/**
 * 校验 request_id 格式是否符合约定：web-UUID
 */
export function isValidRequestId(id: unknown): id is string {
  return typeof id === "string" && /^web-[a-f0-9-]+$/.test(id);
}

// 内存按 owner 暂存快照，用于持久化失败（无痕模式、配额超出等）时不丢原 request_id，保持幂等性
const memorySubmissions = new Map<string, MobileSubmission>();

/** 存储内容不可信；异常快照保留用于核查，不能用于重放或创建替代请求。 */
export function isMobileSubmissionIntact(submission: MobileSubmission): boolean {
  const request = submission.trip_request;
  if (
    !["pending_confirmation", "confirmed"].includes(submission.status) ||
    !isValidRequestId(submission.request_id) ||
    typeof submission.fingerprint !== "string" ||
    !request || typeof request !== "object" || Array.isArray(request) ||
    typeof request.to_city !== "string" || !request.to_city.trim() ||
    !Number.isInteger(request.days) || request.days < 1 || request.days > 7 ||
    !Number.isInteger(request.people_count) || request.people_count < 1 || request.people_count > 6
  ) return false;

  if (request.start_date !== undefined || request.end_date !== undefined) {
    if (!isValidCalendarDate(request.start_date) || !isValidCalendarDate(request.end_date)) return false;
    const days = (Date.parse(request.end_date) - Date.parse(request.start_date)) / 86400000 + 1;
    if (days !== request.days) return false;
  }
  for (const value of [request.preferences, request.avoid]) {
    if (value !== undefined && (!Array.isArray(value) || !value.every(item => typeof item === "string"))) return false;
  }
  for (const value of [request.notes, request.from_city, request.daily_start, request.daily_end]) {
    if (value !== undefined && typeof value !== "string") return false;
  }
  if (request.commute_mode !== undefined && !["driving", "transit", "cycling"].includes(request.commute_mode)) return false;
  if (request.budget !== undefined && (typeof request.budget !== "number" || !Number.isFinite(request.budget) || request.budget < 0)) return false;
  const validPlace = (value: unknown): boolean => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const place = value as Record<string, unknown>;
    return typeof place.name === "string" && Boolean(place.name.trim()) &&
      (place.place_id === undefined || (typeof place.place_id === "number" && Number.isInteger(place.place_id) && place.place_id > 0));
  };
  if (request.must_include !== undefined && (!Array.isArray(request.must_include) || !request.must_include.every(validPlace))) return false;
  if (request.accommodation !== undefined) {
    if (!validPlace(request.accommodation)) return false;
    for (const value of [request.accommodation.latitude, request.accommodation.longitude]) {
      if (value !== undefined && (typeof value !== "number" || !Number.isFinite(value))) return false;
    }
  }
  try {
    return generateFingerprint(request) === submission.fingerprint;
  } catch {
    return false;
  }
}

/**
 * 保存手机提交暂存快照并保持幂等：
 * 相同 owner 且相同 payload fingerprint 时，复用已生成的 request_id；
 * payload 变动或 owner 变动时，生成全新的 web-UUID。
 */
export function saveMobileSubmission(
  request: TripSubmitRequest,
  owner: string,
  draftRevision?: number,
): MobileSubmission {
  const fingerprint = generateFingerprint(request);
  const existing = getMobileSubmission(owner);

  if (existing && !isMobileSubmissionIntact(existing)) {
    throw new Error("原提交记录完整性异常，请先核查原请求状态");
  }

  let requestId: string;
  if (
    existing &&
    existing.owner === owner &&
    existing.fingerprint === fingerprint &&
    isValidRequestId(existing.request_id)
  ) {
    requestId = existing.request_id;
  } else {
    requestId = `web-${crypto.randomUUID()}`;
  }

  const submission: MobileSubmission = {
    owner,
    fingerprint,
    request_id: requestId,
    trip_request: request,
    created_at:
      existing &&
      existing.owner === owner &&
      existing.fingerprint === fingerprint
        ? existing.created_at
        : Date.now(),
    draft_revision: draftRevision,
    status: "pending_confirmation",
    job_id: null,
  };

  // 内存保留 owner 绑定快照
  memorySubmissions.set(owner, submission);

  try {
    sessionStorage.setItem(
      MOBILE_SUBMISSION_KEY,
      JSON.stringify(submission),
    );
  } catch {
    /* 忽略存储异常，保持内存返回值有效 */
  }

  return submission;
}

export function getMobileSubmission(owner?: string): MobileSubmission | null {
  try {
    const raw = sessionStorage.getItem(MOBILE_SUBMISSION_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as MobileSubmission;
      if (
        parsed &&
        typeof parsed === "object" &&
        typeof parsed.owner === "string"
      ) {
        if (!isMobileSubmissionIntact(parsed)) parsed.status = "integrity_error";
        memorySubmissions.set(parsed.owner, parsed);
        if (!owner || parsed.owner === owner) {
          return parsed;
        }
      } else {
        sessionStorage.removeItem(MOBILE_SUBMISSION_KEY);
      }
    }
  } catch {
    try {
      sessionStorage.removeItem(MOBILE_SUBMISSION_KEY);
    } catch {
      /* ignore */
    }
  }

  const allMemory = Array.from(memorySubmissions.values());
  const cached = owner
    ? memorySubmissions.get(owner) || null
    : allMemory.length > 0 ? allMemory[allMemory.length - 1] : null;
  if (cached && !isMobileSubmissionIntact(cached)) cached.status = "integrity_error";
  return cached;
}

export function clearMobileSubmission(owner?: string): void {
  if (owner) {
    memorySubmissions.delete(owner);
  } else {
    memorySubmissions.clear();
  }
  try {
    sessionStorage.removeItem(MOBILE_SUBMISSION_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * 当服务端确认受理并返回有效 jobId 时，更新暂存状态
 */
export function markSubmissionConfirmed(jobId: string, owner?: string): void {
  const current = getMobileSubmission(owner);
  if (current) {
    current.status = "confirmed";
    current.job_id = jobId;
    memorySubmissions.set(current.owner, current);
    try {
      sessionStorage.setItem(
        MOBILE_SUBMISSION_KEY,
        JSON.stringify(current),
      );
    } catch {
      /* ignore */
    }
  }
}

/**
 * 检查当前是否有“结果待确认”的在途提交阻断新请求：
 * 规则：若当前账号存在处于 pending_confirmation 状态的提交，且当前请求 payload 指纹不同，
 * 则禁止发起新的 payload，避免在旧请求未查清前双投。
 */
export function hasBlockingUnknownSubmission(
  owner: string,
  newFingerprint: string,
): boolean {
  const existing = getMobileSubmission(owner);
  if (!existing) return false;
  if (existing.owner !== owner) return false;
  if (!isMobileSubmissionIntact(existing)) return true;
  if (existing.status !== "pending_confirmation") return false;

  // 如果指纹相同，属于“以同一请求重试”，不阻断；指纹不同则必须先核清旧请求
  return existing.fingerprint !== newFingerprint;
}

/**
 * 校验是否满足“以同一请求重试”条件
 */
export function canRetrySameRequest(
  owner: string,
  fingerprint: string,
): boolean {
  const existing = getMobileSubmission(owner);
  if (!existing) return false;
  return (
    existing.owner === owner &&
    existing.fingerprint === fingerprint &&
    isMobileSubmissionIntact(existing) &&
    isValidRequestId(existing.request_id)
  );
}

export const MOBILE_SUBMIT_INTENT_KEY = "yuntu-mobile-submit-intent-v1";

export interface MobileSubmitIntent {
  source: "mobile" | "cinematic";
  draftRevision: number;
  ownerUserId: string | null;
  nonce: string;
  createdAt: number;
}

// 显式权威内存状态机（应对 sessionStorage QuotaExceededError、隐私模式或旧存储残留场景）
type MemoryIntentState =
  | { type: "uninitialized" }
  | { type: "active"; intent: MobileSubmitIntent }
  | { type: "cleared" };

let memoryState: MemoryIntentState = { type: "uninitialized" };

export function isSessionStorageAvailable(): boolean {
  try {
    const testKey = "__yuntu_storage_test__";
    sessionStorage.setItem(testKey, "1");
    sessionStorage.removeItem(testKey);
    return true;
  } catch {
    return false;
  }
}

export function saveMobileSubmitIntent(intent: MobileSubmitIntent): boolean {
  // 当前页面会话内内存状态作为唯一权威事实
  memoryState = {
    type: "active",
    intent: { ...intent },
  };
  try {
    sessionStorage.setItem(MOBILE_SUBMIT_INTENT_KEY, JSON.stringify(intent));
    return true;
  } catch {
    // 写入 storage 失败（如 QuotaExceededError）。
    // 尽最大努力清理旧遗留数据，防止跨会话读取陈旧版本；
    // 无论清理是否成功，当前页面一律以 memoryState 为权威。
    try {
      sessionStorage.removeItem(MOBILE_SUBMIT_INTENT_KEY);
    } catch {
      /* ignore */
    }
    return false;
  }
}

export function getMobileSubmitIntent(): MobileSubmitIntent | null {
  // 1. 若当前页面已明确取消或消费意图，严禁从旧 storage 恢复或复活已作废意图
  if (memoryState.type === "cleared") {
    return null;
  }

  // 2. 若当前页面已有权威内存意图，以内存意图为准（绝不被陈旧 storage 覆盖）
  if (memoryState.type === "active") {
    if (Date.now() - memoryState.intent.createdAt > 15 * 60 * 1000) {
      clearMobileSubmitIntent();
      return null;
    }
    return memoryState.intent;
  }

  // 3. 尚未初始化（新页面会话，例如从第三方 OAuth 重定向回跳或首次加载）：尝试从 sessionStorage 载入
  try {
    const raw = sessionStorage.getItem(MOBILE_SUBMIT_INTENT_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as MobileSubmitIntent;
      if (
        parsed &&
        typeof parsed === "object" &&
        (parsed.source === "mobile" || parsed.source === "cinematic") &&
        typeof parsed.draftRevision === "number" &&
        typeof parsed.nonce === "string" &&
        typeof parsed.createdAt === "number"
      ) {
        if (Date.now() - parsed.createdAt <= 15 * 60 * 1000) {
          memoryState = { type: "active", intent: parsed };
          return parsed;
        }
      }
      sessionStorage.removeItem(MOBILE_SUBMIT_INTENT_KEY);
    }
  } catch {
    /* ignore */
  }

  memoryState = { type: "cleared" };
  return null;
}

export function clearMobileSubmitIntent(): void {
  memoryState = { type: "cleared" };
  try {
    sessionStorage.removeItem(MOBILE_SUBMIT_INTENT_KEY);
  } catch {
    /* ignore */
  }
}

/** 供单测或跨用例重置内存状态，模拟全新独立页面会话 */
export function _resetMobileSubmitIntentState(): void {
  memoryState = { type: "uninitialized" };
}

/**
 * 校验并消费待执行提交意图：
 * - 单次消费：只要读取并校验，立刻从存储中抹除，防止重复恢复或迟到回调再次触发；
 * - 规则：
 *   1. 必须在 15 分钟有效窗口内；
 *   2. source 必须匹配；
 *   3. draftRevision 必须与草稿当前版本一致（若用户在弹窗期间修改了草稿，则作废）；
 *   4. 账号一致性：原为匿名时新登录用户可认领；原已绑定账号但换了新账号则作废。
 */
export function consumeMobileSubmitIntent(
  currentSource: "mobile" | "cinematic",
  currentRevision: number,
  currentUserId: string | null,
): boolean {
  const intent = getMobileSubmitIntent();
  if (!intent) return false;

  // 立即清空，确保单次消费
  clearMobileSubmitIntent();

  if (intent.source !== currentSource) return false;
  if (Date.now() - intent.createdAt > 15 * 60 * 1000) return false;
  if (intent.draftRevision !== currentRevision) return false;

  // 账号检查：如果原意图已有具体账号，但与当前登录账号不同（换号），拒绝
  if (intent.ownerUserId !== null && intent.ownerUserId !== currentUserId) {
    return false;
  }

  return true;
}


