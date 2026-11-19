export interface DayPalette {
  primary: string;
  dark: string;
  lightBg: string;
  border: string;
  text: string;
  outline: string;
}

export const DAY_PALETTES: DayPalette[] = [
  { primary: "#0f766e", dark: "#094e49", lightBg: "#eef9f7", border: "#6ec6bb", text: "#0f766e", outline: "#ffffff" }, // Day 1 翡翠绿
  { primary: "#0284c7", dark: "#0369a1", lightBg: "#f0f9ff", border: "#7dd3fc", text: "#0284c7", outline: "#ffffff" }, // Day 2 晴空蓝
  { primary: "#ea580c", dark: "#c2410c", lightBg: "#fff7ed", border: "#fdba74", text: "#ea580c", outline: "#ffffff" }, // Day 3 暖日橙
  { primary: "#7c3aed", dark: "#6d28d9", lightBg: "#f5f3ff", border: "#c4b5fd", text: "#7c3aed", outline: "#ffffff" }, // Day 4 罗兰紫
  { primary: "#db2777", dark: "#be185d", lightBg: "#fdf2f8", border: "#f472b6", text: "#db2777", outline: "#ffffff" }, // Day 5 珊瑚粉
  { primary: "#ca8a04", dark: "#a16207", lightBg: "#fefce8", border: "#fde047", text: "#ca8a04", outline: "#ffffff" }, // Day 6 琥珀金
  { primary: "#059669", dark: "#047857", lightBg: "#ecfdf5", border: "#6ee7b7", text: "#059669", outline: "#ffffff" }, // Day 7 薄荷绿
];

export function getDayPalette(dayNumber: number): DayPalette {
  const index = Math.max(0, dayNumber - 1) % DAY_PALETTES.length;
  return DAY_PALETTES[index];
}
