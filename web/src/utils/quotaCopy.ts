import type { Quota } from "@/types/auth";

/**
 * 公测期额度扣次规则文案。
 * 必须与 BFF settle_trip 的结算规则保持一致，BFF 改规则时这里要一起改。
 */
export const QUOTA_RULE_TEXT =
  "详细攻略每篇扣 1 次；基础攻略、没排出路线或生成失败都会退回。";

/**
 * limit 是服务端事实：只有拿到大于 0 的有限数字时才认为存在总额度，
 * 前端不给 limit 设默认值，拿不到就不显示总数。
 */
export function hasQuotaLimit(quota: Quota | null | undefined): boolean {
  return (
    typeof quota?.limit === "number" &&
    Number.isFinite(quota.limit) &&
    quota.limit > 0
  );
}

/** 标题：按服务端是否返回 resets_at 区分公测额度与周期性生成额度。 */
export function quotaTitle(quota: Quota | null | undefined): string {
  return quota?.resets_at ? "生成额度" : "公测额度";
}

/** 用量：拿不到 limit 时只报已用次数，不猜总数。 */
export function quotaUsageText(
  quota: Quota | null | undefined,
  empty = "-",
): string {
  if (!quota) return empty;
  if (hasQuotaLimit(quota)) {
    return `已用 ${quota.consumed} / 共 ${quota.limit}`;
  }
  return `已用 ${quota.consumed} 次`;
}

/** 剩余：quota 缺失时不显示任何数字。 */
export function quotaRemainingText(quota: Quota | null | undefined): string {
  if (!quota) return "";
  return `剩余 ${quota.remaining} 次`;
}

/** 有效期：无 resets_at 时是公测期总量，有则显示本地时间的恢复时刻。 */
export function quotaValidityText(quota: Quota | null | undefined): string {
  if (!quota) return "";
  const resetsAt = quota.resets_at;
  if (!resetsAt) return "公测期内有效";
  const date = new Date(resetsAt);
  if (Number.isNaN(date.getTime())) return resetsAt;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())} 恢复额度`;
}
