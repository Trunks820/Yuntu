import { MOBILE_SCENES } from "@/types/mobile";
import type { MobileRhythm } from "@/types/mobile";
import type { Destination, DestinationsResponse } from "@/types/destination";

/** 已选场景的偏好标签（去重，保持选择顺序） */
export function sceneTags(sceneIds: string[]): string[] {
  const tags = sceneIds
    .map((id) => MOBILE_SCENES.find((s) => s.id === id)?.tag)
    .filter((t): t is string => Boolean(t));
  return [...new Set(tags)];
}

export function hasSceneCategory(sceneIds: string[], category: string): boolean {
  return sceneIds.some(
    (id) => MOBILE_SCENES.find((s) => s.id === id)?.category === category,
  );
}

export function rhythmLabel(rhythm: MobileRhythm | null): string {
  return rhythm === "slow" ? "慢一点" : rhythm === "full" ? "充实一点" : "节奏未限定";
}

/**
 * 从 useDestinations 写入的会话缓存里找已选城市，只取封面图、英文名这类装饰信息。
 * 选城步骤已经拉过目录，这里不再订阅查询；缓存缺失时返回 null，调用方自行降级。
 */
export function findCachedDestination(
  cityId: string | null,
  cityName: string | null,
): Destination | null {
  try {
    const cached = JSON.parse(
      sessionStorage.getItem("yuntu:destinations:v1") || "null",
    ) as DestinationsResponse | null;
    return (
      cached?.destinations?.find((d) => d.id === cityId || d.name === cityName) ?? null
    );
  } catch {
    return null;
  }
}

/** 原型 fmtDate：2026-09-25 → 9月25日（withYear 时带年份） */
export function formatMonthDay(date: string, withYear = false): string {
  const [y, m, d] = date.split("-");
  return `${withYear ? `${y}年` : ""}${Number(m)}月${Number(d)}日`;
}


