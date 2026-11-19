export function formatMinutes(minutes: number): string {
  if (minutes < 60) return `${minutes} 分钟`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m > 0 ? `${h} 小时 ${m} 分钟` : `${h} 小时`;
}

export function formatDistance(meters: number): string {
  if (meters < 1000) return `${meters}m`;
  return `${(meters / 1000).toFixed(1)}km`;
}

export function commuteModeName(mode: string): string {
  const map: Record<string, string> = {
    walking: "步行",
    transit: "公交",
    taxi: "打车",
    driving: "驾车",
    cycling: "骑行",
  };
  return map[mode] ?? mode;
}

import type { LucideIcon } from "lucide-react";
import { Footprints, Bus, Car, Bike } from "lucide-react";

/** 出行方式 → Lucide 图标组件。 */
export const COMMUTE_MODE_ICON: Record<string, LucideIcon> = {
  walking: Footprints,
  transit: Bus,
  taxi: Car,
  driving: Car,
  cycling: Bike,
};

/** 出行方式对应图标，未知 mode 落到驾车图标。 */
export function commuteModeIcon(mode: string): LucideIcon {
  return COMMUTE_MODE_ICON[mode] ?? Car;
}

/**
 * 过滤景点 brief 里的生成期占位/指令残留文案，避免脏文案露给用户。
 * 例："写成咖啡/茶歇休息点"、"作为次要活动简洁说明"、"…但不要补充无来源事实"。
 * 命中占位返回空串（调用方据此隐藏）；正常文案原样返回。
 * 同时清除内部排序编号前缀，如"推荐路线第八站，"、"第一天行程第十一站"。
 */
const BRIEF_PLACEHOLDER_RE = /(写成|作为.{0,8}(说明|节点|停留点|展开)|不要补充|无来源事实|简洁说明)/;
const BRIEF_RANK_PREFIX_RE = /^(?:推荐路线)?(?:第[一二三四五六七八九十百零\d]+[天日](?:行程)?)?第[一二三四五六七八九十百零\d]+站[，,、]?\s*/;

export function cleanBrief(brief: string | null | undefined): string {
  if (!brief) return "";
  const t = brief.trim();
  if (BRIEF_PLACEHOLDER_RE.test(t)) return "";
  return t.replace(BRIEF_RANK_PREFIX_RE, "");
}

const TAG_BLACKLIST = new Set([
  "relaxed", "moderate", "intensive", "packed",
  "restaurant", "attraction", "museum", "park",
  "business_area", "hotel", "cafe", "shopping",
  "scenic_spot", "entertainment", "landmark",
]);

/**
 * 与 yuntu-travel export/public_text.py 的 _UNKNOWN_SNAKE_CASE_PATTERN 同一规则：
 * 带下划线的内部代码（如 historic_district）一律不展示。
 * 只是前端兜底，结果 API 的 tags 仍会带出原始 place_type。
 * 用捕获组代替后顾断言，兼容不支持 lookbehind 的旧版 Safari。
 */
const SNAKE_CASE_CODE_RE =
  /(^|[^A-Za-z0-9])(?:_+[a-z0-9]+(?:_[a-z0-9]+)*|[a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![A-Za-z0-9])/gi;
const TAG_EDGE_RE = /^[\s/|·,，;；:=：]+|[\s/|·,，;；:=：]+$/g;

export function cleanTags(tags: string[]): string[] {
  return tags
    .filter((t) => !TAG_BLACKLIST.has(t.toLowerCase()))
    .map((t) => t.replace(SNAKE_CASE_CODE_RE, "$1").replace(TAG_EDGE_RE, ""))
    .filter(Boolean);
}

const SUMMARY_TAIL_RE = /[，,]\s*适合用于对比方案.*$/;
const SUMMARY_DIGIT_BLOB_RE = /[、,，]\s*\d{4,}\s*/g;

export function cleanSummary(summary: string | null | undefined): string {
  if (!summary) return "";
  return summary.trim().replace(SUMMARY_DIGIT_BLOB_RE, "、").replace(SUMMARY_TAIL_RE, "");
}
