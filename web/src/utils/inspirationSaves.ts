/** 「随手收集灵感」的想去清单：后端没有收藏接口，只存在这台设备上，按账号分开 */
export interface InspirationSave {
  placeId: number;
  name: string;
  city: string;
  district: string | null;
  savedAt: number;
}

const STORAGE_PREFIX = "yuntu_mobile_inspiration_v1:";
const SAVE_LIMIT = 200;

function storageKey(userId: string): string {
  return `${STORAGE_PREFIX}${userId}`;
}

function isSave(value: unknown): value is InspirationSave {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.placeId === "number" &&
    Number.isInteger(v.placeId) &&
    v.placeId > 0 &&
    typeof v.name === "string" &&
    v.name.trim() !== "" &&
    typeof v.city === "string" &&
    v.city.trim() !== "" &&
    (v.district === null || typeof v.district === "string") &&
    typeof v.savedAt === "number"
  );
}

export function loadInspirationSaves(userId: string): InspirationSave[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(storageKey(userId)) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    const seen = new Set<string>();
    return parsed.filter(isSave).filter((s) => {
      const key = `${s.city}:${s.placeId}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  } catch {
    return [];
  }
}

/** 返回是否写入成功；浏览器不允许存储时，收藏只在这次打开期间保留 */
export function saveInspirationSaves(userId: string, saves: InspirationSave[]): boolean {
  try {
    localStorage.setItem(storageKey(userId), JSON.stringify(saves.slice(-SAVE_LIMIT)));
    return true;
  } catch {
    return false;
  }
}
