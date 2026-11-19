import type { StopKind } from "@/utils/spreadBook";

// 没实拍的站在图位上画这一类地点的线描；64×64 画布，描边跟随 currentColor
const STOP_KIND_PATHS: Record<StopKind, string[]> = {
  park: [
    "M32 38v14",
    "M19 24a13 12 0 1 0 26 0a13 12 0 1 0 -26 0",
    "M27 30l5 5 5-6",
    "M12 52h40",
    "M41 46h10M43 46v6M49 46v6",
  ],
  food: [
    "M13 33h38a19 19 0 0 1-38 0z",
    "M25 52h14",
    "M39 10l11 19M45 9l10 18",
    "M23 27c-2-3 2-5 0-8M31 27c-2-3 2-5 0-8",
  ],
  museum: [
    "M11 23L32 11l21 12z",
    "M17 27v18M25 27v18M39 27v18M47 27v18",
    "M13 49h38M11 53h42",
  ],
  sight: [
    "M7 51l16-22 9 11 8-9 17 20z",
    "M41 16a5 5 0 1 0 10 0a5 5 0 1 0 -10 0",
  ],
  street: [
    "M7 52h50",
    "M10 52V31l10-8 10 8v21",
    "M30 52V27l12-9 12 9v25",
    "M17 52v-8h6v8M38 36h8v6h-8z",
    "M20 23v-4M42 18v-5",
  ],
  market: [
    "M11 21h42l-4 11H15z",
    "M15 32a4.5 4.5 0 0 0 8.5 0a4.5 4.5 0 0 0 8.5 0a4.5 4.5 0 0 0 8.5 0a4.5 4.5 0 0 0 8.5 0",
    "M15 34v18M49 34v18M11 52h42",
    "M23 47a3 3 0 1 0 6 0a3 3 0 1 0 -6 0M30 47a3 3 0 1 0 6 0a3 3 0 1 0 -6 0M37 47a3 3 0 1 0 6 0a3 3 0 1 0 -6 0",
  ],
  photo: [
    "M12 22v-8h8M44 14h8v8M52 42v8h-8M20 50h-8v-8",
    "M24 32a8 8 0 1 0 16 0a8 8 0 1 0 -16 0",
    "M30 32a2 2 0 1 0 4 0a2 2 0 1 0 -4 0",
  ],
  other: [
    "M15 32a17 17 0 1 0 34 0a17 17 0 1 0 -34 0",
    "M32 17l5 15-5 15-5-15z",
    "M32 11v3M32 50v3M11 32h3M50 32h3",
  ],
};

export function StopKindMark({ kind }: { kind: StopKind }) {
  return (
    <svg
      className="bk-kind-mark"
      viewBox="0 0 64 64"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {STOP_KIND_PATHS[kind].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
