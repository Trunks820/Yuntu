/**
 * Concept scene art was not in the sync pack (webp assets excluded).
 * These SVG placeholders keep the mobile cards rendering without a private fetch.
 * City identity photos still come from the public destination catalog.
 */

function scene(label: string, start: string, end: string): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1000" viewBox="0 0 800 1000"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${start}"/><stop offset="1" stop-color="${end}"/></linearGradient></defs><rect width="800" height="1000" fill="url(#g)"/><text x="48" y="920" fill="#fffef9" font-size="42" font-family="sans-serif">${label}</text></svg>`;
  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
}

export const CONCEPT_ASSETS = {
  lake: scene("山水", "#7d9b8a", "#143c35"),
  forest: scene("林间", "#4c9b80", "#0f2f2a"),
  rooftops: scene("屋顶", "#c9a394", "#5d3328"),
  alley: scene("老街", "#b57e6b", "#143c35"),
  night: scene("夜色", "#173b35", "#0a211e"),
  food: scene("小馆", "#a35b48", "#48271e"),
  coast: scene("海岸", "#93bfa9", "#166c5b"),
  cafe: scene("茶歇", "#dce9d6", "#166c5b"),
} as const;

export type ConceptImageKey = keyof typeof CONCEPT_ASSETS;

export const HOME_HERO_IMAGE = CONCEPT_ASSETS.lake;
