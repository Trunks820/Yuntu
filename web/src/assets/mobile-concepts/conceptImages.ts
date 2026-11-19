import alley from "./alley.webp";
import cafe from "./cafe.webp";
import coast from "./coast.webp";
import food from "./food.webp";
import forest from "./forest.webp";
import lake from "./lake.webp";
import night from "./night.webp";
import rooftops from "./rooftops.webp";

/**
 * 云途手机端概念场景图。
 * 从冻结原型内嵌的 8 张 webp 原样提取，只表达旅行氛围，不代表任何城市或 POI 实景；
 * 城市身份图继续走真实目录 / CDN。
 */
export const CONCEPT_ASSETS = {
  lake,
  forest,
  rooftops,
  alley,
  night,
  food,
  coast,
  cafe,
} as const;

export type ConceptImageKey = keyof typeof CONCEPT_ASSETS;

export const HOME_HERO_IMAGE = CONCEPT_ASSETS.lake;
