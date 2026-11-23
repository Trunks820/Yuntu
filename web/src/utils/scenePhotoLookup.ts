import {
  SCENE_PHOTOS_BY_ID,
  type ScenePhotoRecord,
} from "@/utils/scenePhotos.generated";

/** 按 Unsplash 图片编号取已登记的场景图；清单里没有就直接报错，不悄悄退回小图 */
export function scenePhotoByPhotoId(photoId: string): ScenePhotoRecord {
  for (const photos of Object.values(SCENE_PHOTOS_BY_ID)) {
    const hit = photos.find((p) => p.photoId === photoId);
    if (hit) return hit;
  }
  throw new Error(
    `scene photo ${photoId} is not registered in scene-photo-manifest.json`,
  );
}

/** 手机端整幅图：普通屏用 960 宽，高清屏用 1920 宽 */
export function sceneSrcSet(photo: ScenePhotoRecord): string {
  return `${photo.mobileUrl} 960w, ${photo.desktopUrl} 1920w`;
}
