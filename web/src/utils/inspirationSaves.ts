export interface InspirationSave {
  city: string;
  placeId: number;
  name: string;
  district?: string | null;
  savedAt?: number;
}

const KEY = "yuntu-inspiration-saves-v1";

function bucket(owner: string): InspirationSave[] {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}") as Record<string, InspirationSave[]>;
    const rows = raw[owner];
    if (!Array.isArray(rows)) return [];
    return rows.filter(
      (row) =>
        row &&
        typeof row.city === "string" &&
        typeof row.name === "string" &&
        Number.isInteger(row.placeId),
    );
  } catch {
    return [];
  }
}

export function loadInspirationSaves(owner: string): InspirationSave[] {
  return bucket(owner);
}

/** Returns false when the browser refuses to store the list. */
export function saveInspirationSaves(owner: string, saves: InspirationSave[]): boolean {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}") as Record<string, unknown>;
    raw[owner] = saves;
    localStorage.setItem(KEY, JSON.stringify(raw));
    return true;
  } catch {
    return false;
  }
}
