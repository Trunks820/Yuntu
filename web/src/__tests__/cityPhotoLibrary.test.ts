import { describe, expect, it } from "vitest";
import {
  cityImageList,
  cityNameOfImage,
} from "@/components/input/RotatingBackground";
import { CITY_NAME_TO_FOLDER } from "@/utils/cityPhotos";

const SUPPORTED_CITIES = Object.keys(CITY_NAME_TO_FOLDER);

const CITY_FOLDERS = [
  ["北京", "beijing"],
  ["上海", "shanghai"],
  ["重庆", "chongqing"],
  ["成都", "chengdu"],
  ["杭州", "hangzhou"],
  ["西安", "xian"],
  ["南京", "nanjing"],
  ["长沙", "changsha"],
  ["青岛", "qingdao"],
  ["桂林", "guilin"],
  ["广州", "guangzhou"],
  ["武汉", "wuhan"],
  ["苏州", "suzhou"],
  ["厦门", "xiamen"],
  ["昆明", "kunming"],
  ["三亚", "sanya"],
] as const;

describe("city photo library", () => {
  it("keeps the six-city expansion in the controlled city selector", () => {
    expect(SUPPORTED_CITIES).toHaveLength(16);
    expect(SUPPORTED_CITIES).toEqual(
      expect.arrayContaining(["广州", "武汉", "苏州", "厦门", "昆明", "三亚"]),
    );
  });

  it.each(CITY_FOLDERS)("uses bundled Chongqing fallback for %s when no CDN is set", (city, folder) => {
    const images = cityImageList(city);

    expect(images).toEqual(["/bundled/chongqing.png"]);
    if (folder === "chongqing") {
      expect(cityNameOfImage(images[0])).toBe("重庆");
    }
  });
});
