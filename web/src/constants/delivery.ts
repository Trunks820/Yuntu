/**
 * v0.12 交付与结算状态展示映射。
 *
 * 所有用户可见文案统一从此文件获取，确保 Result / History / TaskNotice 一致。
 * 文案来源：BFF 契约 §6 消费端文案映射。
 */
import type {
  DeliveryOutcome,
  QuotaState,
} from "@/types/trip";
import type { HistoryTripItem } from "@/types/auth";

/* ---------- outcome → 用户文案 ---------- */

export interface OutcomeDisplay {
  label: string;
  color: "emerald" | "amber" | "red" | "blue" | "gray";
  icon: "check" | "info" | "alert" | "search" | "clock" | "help";
}

const OUTCOME_DISPLAY: Record<DeliveryOutcome, OutcomeDisplay> = {
  PENDING: {
    label: "生成中",
    color: "blue",
    icon: "clock",
  },
  DETAILED: {
    label: "详细攻略已生成",
    color: "emerald",
    icon: "check",
  },
  BASIC: {
    label: "基础行程已生成",
    color: "amber",
    icon: "info",
  },
  NO_ROUTE: {
    label: "未找到合规路线",
    color: "amber",
    icon: "search",
  },
  FAILED: {
    label: "生成失败",
    color: "red",
    icon: "alert",
  },
  UNKNOWN: {
    label: "结果状态待确认",
    color: "gray",
    icon: "help",
  },
};

export function getOutcomeDisplay(outcome: DeliveryOutcome): OutcomeDisplay {
  return OUTCOME_DISPLAY[outcome] ?? OUTCOME_DISPLAY.UNKNOWN;
}

/* ---------- quota_state → 结算说明 ---------- */

const QUOTA_NOTICES: Record<QuotaState, string | null> = {
  RELEASED: "本次不扣额度",
  CONSUMED: "已使用 1 次生成额度",
  RESERVED: "额度已预占，结算待确认",
  UNKNOWN: "额度状态待确认",
  NOT_APPLICABLE: null, // 不展示
};

export function getQuotaNotice(quotaState: QuotaState): string | null {
  return QUOTA_NOTICES[quotaState] ?? null;
}

/* ---------- NO_ROUTE reason_code → 用户说明 ---------- */

/**
 * search_budget_exhausted 只说明本次限定搜索未找到方案，
 * 不能说城市无路线、用户条件必定冲突，或硬编码"减少天数"。
 */
export function getNoRouteExplanation(reasonCode: string | null): string {
  switch (reasonCode) {
    case "SEARCH_BUDGET_EXHAUSTED":
      return "本次搜索在限定范围内未找到符合条件的方案";
    case "CONSTRAINT_CONFLICT":
      return "当前条件组合暂时无法规划出合规路线";
    case "NO_CANDIDATES":
      return "未能检索到可用的候选路线";
    case "ROUTE_DATA_UNAVAILABLE":
      return "路线数据暂时不可用";
    case "NO_USABLE_ROUTE":
      return "未找到可用的合规路线";
    default:
      return "未找到合规路线";
  }
}

/* ---------- 历史列表展示状态推导 ---------- */

/**
 * 在历史 UI 中用于统一七类状态呈现的枚举。
 * 不是 BFF 字段，纯展示用。
 */
export type HistoryDisplayStatus =
  | "detailed"
  | "basic"
  | "no_route"
  | "failed"
  | "pending"
  | "unknown"
  | "legacy_success"
  | "legacy_failed";

export interface HistoryDisplayInfo {
  status: HistoryDisplayStatus;
  outcome: OutcomeDisplay;
  quotaNotice: string | null;
}

/**
 * 从 HistoryTripItem 推导展示状态。
 * legacy 缺 delivery 时按历史展示，不猜免费或详细。
 */
export function resolveHistoryDisplay(
  item: Pick<HistoryTripItem, "delivery" | "result_record_id"> & {
    status: string;
    generation_policy_version?: string | null;
  },
): HistoryDisplayInfo {
  const delivery = item.delivery;

  // v0.12 任务有 delivery
  if (delivery && delivery.outcome) {
    const outcome = getOutcomeDisplay(delivery.outcome);
    const quotaNotice = getQuotaNotice(delivery.quota_state);
    let status: HistoryDisplayStatus;
    switch (delivery.outcome) {
      case "DETAILED":
        status = "detailed";
        break;
      case "BASIC":
        status = "basic";
        break;
      case "NO_ROUTE":
        status = "no_route";
        break;
      case "FAILED":
        status = "failed";
        break;
      case "PENDING":
        status = "pending";
        break;
      default:
        status = "unknown";
    }
    return { status, outcome, quotaNotice };
  }

  const policy = item.generation_policy_version;
  const isLegacy = policy == null || policy === "legacy";

  if (isLegacy) {
    if (item.status === "SUCCESS") {
      return {
        status: "legacy_success",
        outcome: {
          label: "攻略已生成",
          color: "emerald",
          icon: "check",
        },
        quotaNotice: null,
      };
    }
    if ((item.status as string) === "RUNNING" || (item.status as string) === "SUBMITTED" || (item.status as string) === "PENDING") {
      return {
        status: "pending",
        outcome: getOutcomeDisplay("PENDING"),
        quotaNotice: null,
      };
    }

    return {
      status: "legacy_failed",
      outcome: {
        label: "生成未就绪",
        color: "red",
        icon: "alert",
      },
      quotaNotice: null,
    };
  }

  // v0.12+（或未知版本）但缺失 delivery
  if ((item.status as string) === "RUNNING" || (item.status as string) === "SUBMITTED" || (item.status as string) === "PENDING") {
    return {
      status: "pending",
      outcome: getOutcomeDisplay("PENDING"),
      quotaNotice: null,
    };
  }

  return {
    status: "unknown",
    outcome: getOutcomeDisplay("UNKNOWN"),
    quotaNotice: null,
  };
}
