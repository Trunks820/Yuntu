// Generated from docs/assets/scene-photo-manifest.json via scripts/generate-scene-photos.mjs.
// Do not edit manually.

import { CITY_ASSET_CDN_BASE_URL, usesBundledCityAssets } from "@/config/cityAssets";
import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";

const SCENE_ASSET_BASE = (
  import.meta.env.VITE_CITY_ASSET_BASE_URL ?? CITY_ASSET_CDN_BASE_URL
)
  .trim()
  .replace(/\/+$/, "");

function resolveSceneUrl(key: string, fallback: string): string {
  return usesBundledCityAssets(SCENE_ASSET_BASE)
    ? fallback
    : `${SCENE_ASSET_BASE}/${key}`;
}

export interface ScenePhotoRecord {
  sceneOrder: number;
  photoId: string;
  desktopKey: string;
  mobileKey: string;
  desktopUrl: string;
  mobileUrl: string;
  credit: string;
}

export const SCENE_PHOTOS_BY_ID: Record<string, readonly ScenePhotoRecord[]> = {
  "street_1": [
    {
      sceneOrder: 0,
      photoId: "0R2rHjjKWxM",
      desktopKey: "scene-opt/dominic-kurniawan-suryaputra-0R2rHjjKWxM-unsplash.webp",
      mobileKey: "scene-opt/dominic-kurniawan-suryaputra-0R2rHjjKWxM-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/dominic-kurniawan-suryaputra-0R2rHjjKWxM-unsplash.webp", CONCEPT_ASSETS.alley),
      mobileUrl: resolveSceneUrl("scene-opt/dominic-kurniawan-suryaputra-0R2rHjjKWxM-unsplash.mobile.webp", CONCEPT_ASSETS.alley),
      credit: "摄影 Dominic Kurniawan Suryaputra · Unsplash",
    },
  ],
  "food_1": [
    {
      sceneOrder: 0,
      photoId: "zy92yz3sW0E",
      desktopKey: "scene-opt/yihan-wang-zy92yz3sW0E-unsplash.webp",
      mobileKey: "scene-opt/yihan-wang-zy92yz3sW0E-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/yihan-wang-zy92yz3sW0E-unsplash.webp", CONCEPT_ASSETS.food),
      mobileUrl: resolveSceneUrl("scene-opt/yihan-wang-zy92yz3sW0E-unsplash.mobile.webp", CONCEPT_ASSETS.food),
      credit: "摄影 Yihan Wang · Unsplash",
    },
  ],
  "food_2": [
    {
      sceneOrder: 0,
      photoId: "Q3ONsRgRlUc",
      desktopKey: "scene-opt/fenghua-Q3ONsRgRlUc-unsplash.webp",
      mobileKey: "scene-opt/fenghua-Q3ONsRgRlUc-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/fenghua-Q3ONsRgRlUc-unsplash.webp", CONCEPT_ASSETS.cafe),
      mobileUrl: resolveSceneUrl("scene-opt/fenghua-Q3ONsRgRlUc-unsplash.mobile.webp", CONCEPT_ASSETS.cafe),
      credit: "摄影 Fenghua · Unsplash",
    },
    {
      sceneOrder: 1,
      photoId: "3t04amJQ2N8",
      desktopKey: "scene-opt/zhe-zhang-3t04amJQ2N8-unsplash.webp",
      mobileKey: "scene-opt/zhe-zhang-3t04amJQ2N8-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/zhe-zhang-3t04amJQ2N8-unsplash.webp", CONCEPT_ASSETS.food),
      mobileUrl: resolveSceneUrl("scene-opt/zhe-zhang-3t04amJQ2N8-unsplash.mobile.webp", CONCEPT_ASSETS.food),
      credit: "摄影 Zhe ZHANG · Unsplash",
    },
    {
      sceneOrder: 2,
      photoId: "KWFGbAv6Ihk",
      desktopKey: "scene-opt/yang-miao-KWFGbAv6Ihk-unsplash.webp",
      mobileKey: "scene-opt/yang-miao-KWFGbAv6Ihk-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/yang-miao-KWFGbAv6Ihk-unsplash.webp", CONCEPT_ASSETS.cafe),
      mobileUrl: resolveSceneUrl("scene-opt/yang-miao-KWFGbAv6Ihk-unsplash.mobile.webp", CONCEPT_ASSETS.cafe),
      credit: "摄影 yang miao · Unsplash",
    },
  ],
  "night_2": [
    {
      sceneOrder: 0,
      photoId: "q6UEdNEGrtQ",
      desktopKey: "scene-opt/nick-h-q6UEdNEGrtQ-unsplash.webp",
      mobileKey: "scene-opt/nick-h-q6UEdNEGrtQ-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/nick-h-q6UEdNEGrtQ-unsplash.webp", CONCEPT_ASSETS.night),
      mobileUrl: resolveSceneUrl("scene-opt/nick-h-q6UEdNEGrtQ-unsplash.mobile.webp", CONCEPT_ASSETS.night),
      credit: "摄影 Nick H · Unsplash",
    },
    {
      sceneOrder: 1,
      photoId: "GRLtoDYun8Q",
      desktopKey: "scene-opt/sergio-kian-GRLtoDYun8Q-unsplash.webp",
      mobileKey: "scene-opt/sergio-kian-GRLtoDYun8Q-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/sergio-kian-GRLtoDYun8Q-unsplash.webp", CONCEPT_ASSETS.rooftops),
      mobileUrl: resolveSceneUrl("scene-opt/sergio-kian-GRLtoDYun8Q-unsplash.mobile.webp", CONCEPT_ASSETS.rooftops),
      credit: "摄影 Sergio Kian · Unsplash",
    },
    {
      sceneOrder: 2,
      photoId: "Du5FXCrcW9c",
      desktopKey: "scene-opt/lastmayday-Du5FXCrcW9c-unsplash.webp",
      mobileKey: "scene-opt/lastmayday-Du5FXCrcW9c-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/lastmayday-Du5FXCrcW9c-unsplash.webp", CONCEPT_ASSETS.night),
      mobileUrl: resolveSceneUrl("scene-opt/lastmayday-Du5FXCrcW9c-unsplash.mobile.webp", CONCEPT_ASSETS.night),
      credit: "摄影 lastmayday · Unsplash",
    },
    {
      sceneOrder: 3,
      photoId: "sRy2kBQCNts",
      desktopKey: "scene-opt/toby-yang-sRy2kBQCNts-unsplash.webp",
      mobileKey: "scene-opt/toby-yang-sRy2kBQCNts-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/toby-yang-sRy2kBQCNts-unsplash.webp", CONCEPT_ASSETS.lake),
      mobileUrl: resolveSceneUrl("scene-opt/toby-yang-sRy2kBQCNts-unsplash.mobile.webp", CONCEPT_ASSETS.lake),
      credit: "摄影 Toby Yang · Unsplash",
    },
    {
      sceneOrder: 4,
      photoId: "Y-HwuSKt6gM",
      desktopKey: "scene-opt/diem-nhi-nguyen-Y-HwuSKt6gM-unsplash.webp",
      mobileKey: "scene-opt/diem-nhi-nguyen-Y-HwuSKt6gM-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/diem-nhi-nguyen-Y-HwuSKt6gM-unsplash.webp", CONCEPT_ASSETS.night),
      mobileUrl: resolveSceneUrl("scene-opt/diem-nhi-nguyen-Y-HwuSKt6gM-unsplash.mobile.webp", CONCEPT_ASSETS.night),
      credit: "摄影 Diem Nhi Nguyen · Unsplash",
    },
    {
      sceneOrder: 5,
      photoId: "XqtoNK6BETo",
      desktopKey: "scene-opt/atul-vinayak-XqtoNK6BETo-unsplash.webp",
      mobileKey: "scene-opt/atul-vinayak-XqtoNK6BETo-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/atul-vinayak-XqtoNK6BETo-unsplash.webp", CONCEPT_ASSETS.forest),
      mobileUrl: resolveSceneUrl("scene-opt/atul-vinayak-XqtoNK6BETo-unsplash.mobile.webp", CONCEPT_ASSETS.forest),
      credit: "摄影 Atul Vinayak · Unsplash",
    },
  ],
  "street_3": [
    {
      sceneOrder: 0,
      photoId: "BdoKOXaKv48",
      desktopKey: "scene-opt/preillumination-seth-BdoKOXaKv48-unsplash.webp",
      mobileKey: "scene-opt/preillumination-seth-BdoKOXaKv48-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/preillumination-seth-BdoKOXaKv48-unsplash.webp", CONCEPT_ASSETS.coast),
      mobileUrl: resolveSceneUrl("scene-opt/preillumination-seth-BdoKOXaKv48-unsplash.mobile.webp", CONCEPT_ASSETS.coast),
      credit: "摄影 Preillumination SeTh · Unsplash",
    },
  ],
  "food_3": [
    {
      sceneOrder: 0,
      photoId: "lny58iNNdHc",
      desktopKey: "scene-opt/panpan-ma-lny58iNNdHc-unsplash.webp",
      mobileKey: "scene-opt/panpan-ma-lny58iNNdHc-unsplash.mobile.webp",
      desktopUrl: resolveSceneUrl("scene-opt/panpan-ma-lny58iNNdHc-unsplash.webp", CONCEPT_ASSETS.food),
      mobileUrl: resolveSceneUrl("scene-opt/panpan-ma-lny58iNNdHc-unsplash.mobile.webp", CONCEPT_ASSETS.food),
      credit: "摄影 panpan ma · Unsplash",
    },
  ],
};
