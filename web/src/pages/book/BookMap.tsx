/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import AMapLoader from "@amap/amap-jsapi-loader";
import type {
  AccommodationInfo,
  TripDay,
  TripPlace,
  WeatherDay,
} from "@/types/trip";
import {
  computeBookMapAvoidPadding,
  DEFAULT_BOOK_STRIP_FOLDED_HEIGHT,
  DEFAULT_BOOK_STRIP_HEIGHT,
  hasValidAccommodationCoords,
  isReducedMotionPreferred,
  isValidGeoPoint,
  resolveBookMapWeatherVisual,
  type BookMapAvoidMetrics,
  type SunDropPoint,
} from "@/utils/spreadBook";
import { BookWeather } from "./BookWeather";

const AMAP_LOAD_TIMEOUT_MS = 25_000;
// 跨区 setFitView 动画实测约 1.1–1.3s，兜底超时需长于该时长，避免动画未落定就抢跑 panTo
const FIT_VIEW_SETTLE_CAP_MS = 1600;
let inFlightAmapLoad: Promise<any> | null = null;
let inFlightAmapToken: object | null = null;

/**
 * React StrictMode 双挂载或网络失败时复用同一次 SDK 加载，
 * 并在失败或超时后重置 @amap/amap-jsapi-loader 内部状态以便后续重试。
 */
function loadAmapSdk(key: string): Promise<any> {
  if (typeof window !== "undefined" && (window as any).AMap) {
    return Promise.resolve((window as any).AMap);
  }
  if (inFlightAmapLoad) {
    return inFlightAmapLoad;
  }

  let timeoutId: number | null = null;
  let settled = false;
  const token = {};
  inFlightAmapToken = token;

  const cleanup = () => {
    if (settled) return;
    settled = true;
    if (timeoutId !== null) {
      window.clearTimeout(timeoutId);
      timeoutId = null;
    }
    if (inFlightAmapToken === token) {
      inFlightAmapToken = null;
      inFlightAmapLoad = null;
    }
  };

  const resetOnFailure = () => {
    if (typeof window !== "undefined" && !(window as any).AMap) {
      (AMapLoader as any).reset?.();
    }
  };

  const loadPromise = new Promise<any>((resolve, reject) => {
    timeoutId = window.setTimeout(() => {
      if (settled) return;
      cleanup();
      resetOnFailure();
      reject(new Error("AMap SDK load timed out"));
    }, AMAP_LOAD_TIMEOUT_MS);

    Promise.resolve(AMapLoader.load({ key, version: "2.0" }))
      .then((AMap) => {
        if (settled) return;
        cleanup();
        if (!AMap) {
          reject(new Error("AMap SDK unavailable"));
          return;
        }
        resolve(AMap);
      })
      .catch((err) => {
        if (settled) return;
        cleanup();
        resetOnFailure();
        reject(err);
      });
  });

  inFlightAmapLoad = loadPromise;
  return loadPromise;
}

interface PlaceMarkerEntry {
  placeId: number;
  marker: any;
  btn: HTMLButtonElement;
  labelEl: HTMLElement;
  baseZIndex: number;
  place: TripPlace;
}

interface StayMarkerEntry {
  marker: any;
  btn: HTMLButtonElement;
  labelEl: HTMLElement;
  accommodation: AccommodationInfo;
}

export interface BookMapSettledSnapshot {
  dayNum: number;
  stageWidth: number;
  stageHeight: number;
  hasSidePanel: boolean;
  isReading: boolean;
  avoidPoints: SunDropPoint[];
}

export interface BookMapProps {
  day: TripDay;
  accommodation?: AccommodationInfo | null;
  weatherDay?: WeatherDay | null;
  selectedPlaceId: number | null;
  selectedStay?: boolean;
  hasSidePanel: boolean;
  isReading?: boolean;
  stripVisibleHeight?: number;
  stripFolded?: boolean;
  sideWidth?: number;
  onSelectPlace: (placeId: number) => void;
  onSelectStay?: () => void;
  onCameraSettling?: () => void;
  onCameraSettled?: (snapshot: BookMapSettledSnapshot) => void;
}

function buildOverlaySignature(
  targetDay: TripDay,
  targetAccommodation: AccommodationInfo | null | undefined,
): string {
  const stopPart = targetDay.places
    .map(
      (p) =>
        `${p.place_id}:${p.name}:${p.optional ? 1 : 0}:${p.longitude},${p.latitude}`,
    )
    .join("|");
  const stayPart = hasValidAccommodationCoords(targetAccommodation)
    ? `${targetAccommodation.longitude},${targetAccommodation.latitude}`
    : "none";
  return `${targetDay.day}#${stopPart}#${stayPart}`;
}

function readContainerPx(pt: any): { x: number; y: number } | null {
  if (!pt) return null;
  const x = typeof pt.getX === "function" ? pt.getX() : pt.x;
  const y = typeof pt.getY === "function" ? pt.getY() : pt.y;
  if (typeof x !== "number" || typeof y !== "number") return null;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x, y };
}

function nudgePointIntoClearArea(
  AMap: any,
  map: any,
  longitude: number,
  latitude: number,
  fallbackLabelWidth: number,
  labelEl: HTMLElement | null,
  container: HTMLElement,
  hasSidePanel: boolean,
  isReading: boolean,
  reducedMotion: boolean,
  avoidMetrics?: BookMapAvoidMetrics,
): boolean {
  if (
    !isValidGeoPoint(longitude, latitude) ||
    typeof map.lngLatToContainer !== "function" ||
    typeof map.containerToLngLat !== "function"
  ) {
    return false;
  }

  const w = container.clientWidth || window.innerWidth;
  const h = container.clientHeight || Math.max(300, window.innerHeight - 52);
  if (w <= 0 || h <= 0) return false;

  const px = readContainerPx(map.lngLatToContainer([longitude, latitude]));
  if (!px) return false;

  const measuredLabelW = Math.ceil(labelEl?.getBoundingClientRect().width || 0);
  // 读攻略态标签前面带“正在读 · ”四个字，未布局完成或 jsdom 下按更宽兜底预留
  const fallbackWithReading = isReading
    ? Math.max(fallbackLabelWidth, 144)
    : fallbackLabelWidth;
  const rawLabelW = measuredLabelW > 0 ? measuredLabelW : fallbackWithReading;
  const labelClearance = 20 + Math.min(isReading ? 240 : 180, rawLabelW) + 20;
  const measuredSideW = avoidMetrics?.sideWidth;
  const resolvedSideW =
    !isReading && !hasSidePanel
      ? 0
      : typeof measuredSideW === "number" &&
          Number.isFinite(measuredSideW) &&
          measuredSideW > 0
        ? measuredSideW
        : isReading
          ? Math.max(0, Math.min(540, w - 380))
          : 380;
  const rightGutter = resolvedSideW > 0 ? resolvedSideW + 28 : 24;
  const safeRight = Math.max(120, w - rightGutter - labelClearance);
  const stripFolded = Boolean(avoidMetrics?.stripFolded);
  const measuredStripH = avoidMetrics?.stripVisibleHeight;
  const resolvedStripH =
    typeof measuredStripH === "number" &&
    Number.isFinite(measuredStripH) &&
    measuredStripH > 0
      ? measuredStripH
      : stripFolded
        ? DEFAULT_BOOK_STRIP_FOLDED_HEIGHT
        : DEFAULT_BOOK_STRIP_HEIGHT;
  const stripClear = stripFolded ? resolvedStripH : resolvedStripH + 16;
  const bottomMargin = isReading ? 64 : stripClear + 34;
  const safeBottom = Math.max(140, h - bottomMargin);
  const safeTop = 96;
  const inLeftHudCorner = px.x < 368 && px.y < 300;
  const watermarkCutY = h - (bottomMargin + 20);
  const inWatermarkStrip = px.x < 290 && px.y > watermarkCutY;
  const coveredRight = px.x > safeRight;
  const coveredBottom = px.y > safeBottom || inWatermarkStrip;
  const coveredTop = px.y < safeTop;
  const coveredLeft = px.x < 40;
  const offscreen = px.x < 0 || px.x > w || px.y < 0 || px.y > h;

  if (
    !offscreen &&
    !inLeftHudCorner &&
    !coveredRight &&
    !coveredBottom &&
    !coveredTop &&
    !coveredLeft
  ) {
    return false;
  }

  const clearLeft = Math.min(372, safeRight - 40);
  if (safeRight <= clearLeft || safeBottom <= safeTop) return false;

  let targetX: number;
  let targetY: number;
  if (offscreen) {
    // 原本不在当前视野里（如从动物园看解放碑住处）：把目标挪进不被浮层/底卷/右抽屉挡住的开阔中央，与原型 panTo 效果一致
    targetX = Math.max(clearLeft, Math.min(safeRight, Math.round(w / 2)));
    const minCenterY = Math.min(safeTop + 16, safeBottom);
    const maxCenterY = Math.max(minCenterY, safeBottom - 16);
    targetY = Math.max(minCenterY, Math.min(maxCenterY, Math.round(h / 2)));
  } else {
    // 原本在画布内、只被局部浮层挡住：让开少量边距，保留用户当前的地图视角
    const nudgeMinY = Math.min(safeTop + 12, safeBottom);
    const rawMaxY =
      px.x < 290 ? Math.min(safeBottom, watermarkCutY) : safeBottom;
    const nudgeMaxY = Math.max(nudgeMinY, rawMaxY - 12);
    targetY = Math.max(nudgeMinY, Math.min(nudgeMaxY, px.y));
    const nudgeMinX = targetY < 300 ? clearLeft : 56;
    const nudgeMaxX = Math.max(nudgeMinX, safeRight - 12);
    targetX = Math.max(nudgeMinX, Math.min(nudgeMaxX, px.x));
  }
  const nextCenterPx = new AMap.Pixel(
    w / 2 + (px.x - targetX),
    h / 2 + (px.y - targetY),
  );
  const nextCenter = map.containerToLngLat(nextCenterPx);
  if (!nextCenter) return false;

  if (reducedMotion && typeof map.setCenter === "function") {
    map.setCenter(nextCenter, true);
    return false;
  }
  if (typeof map.panTo === "function") {
    map.panTo(nextCenter);
    return true;
  }
  if (typeof map.setCenter === "function") {
    map.setCenter(nextCenter, reducedMotion);
  }
  return false;
}

export function BookMap({
  day,
  accommodation,
  weatherDay = null,
  selectedPlaceId,
  selectedStay = false,
  hasSidePanel,
  isReading = false,
  stripVisibleHeight = 0,
  stripFolded = false,
  sideWidth = 0,
  onSelectPlace,
  onSelectStay,
  onCameraSettling,
  onCameraSettled,
}: BookMapProps) {
  const stageRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const amapRef = useRef<any>(null);
  const mapRef = useRef<any>(null);
  const placeEntriesRef = useRef<PlaceMarkerEntry[]>([]);
  const stayEntryRef = useRef<StayMarkerEntry | null>(null);
  const extraOverlaysRef = useRef<any[]>([]);
  const [mapReady, setMapReady] = useState(false);
  const [mapFailed, setMapFailed] = useState(false);

  const dayRef = useRef(day);
  dayRef.current = day;
  const accommodationRef = useRef(accommodation);
  accommodationRef.current = accommodation;
  const selectedPlaceIdRef = useRef(selectedPlaceId);
  selectedPlaceIdRef.current = selectedPlaceId;
  const selectedStayRef = useRef(selectedStay);
  selectedStayRef.current = selectedStay;
  const hasSidePanelRef = useRef(hasSidePanel);
  hasSidePanelRef.current = hasSidePanel;
  const isReadingRef = useRef(isReading);
  isReadingRef.current = isReading;
  const stripVisibleHeightRef = useRef(stripVisibleHeight);
  stripVisibleHeightRef.current = stripVisibleHeight;
  const stripFoldedRef = useRef(stripFolded);
  stripFoldedRef.current = stripFolded;
  const sideWidthRef = useRef(sideWidth);
  sideWidthRef.current = sideWidth;
  const onSelectPlaceRef = useRef(onSelectPlace);
  onSelectPlaceRef.current = onSelectPlace;
  const onSelectStayRef = useRef(onSelectStay);
  onSelectStayRef.current = onSelectStay;
  const onCameraSettlingRef = useRef(onCameraSettling);
  onCameraSettlingRef.current = onCameraSettling;
  const onCameraSettledRef = useRef(onCameraSettled);
  onCameraSettledRef.current = onCameraSettled;

  const weatherVisual = useMemo(
    () => resolveBookMapWeatherVisual(weatherDay?.icon_code),
    [weatherDay?.icon_code],
  );

  const prevDayNumberRef = useRef<number | null>(null);
  const prevIsReadingRef = useRef<boolean>(isReading);
  const prevStripFoldedRef = useRef<boolean>(stripFolded);
  const prevStripVisibleHeightRef = useRef<number>(stripVisibleHeight);
  const prevHasSidePanelRef = useRef<boolean>(hasSidePanel);
  const fitSettlingUntilRef = useRef<number>(0);
  const renderedOverlaySigRef = useRef<string>("");
  const lastPinActivationRef = useRef<{ placeId: number; at: number } | null>(
    null,
  );
  const lastStayBtnAtRef = useRef<number>(-1000);

  const activatePin = useCallback((placeId: number) => {
    const now =
      typeof performance !== "undefined" ? performance.now() : Date.now();
    const last = lastPinActivationRef.current;
    if (last && last.placeId === placeId && now - last.at < 40) {
      return;
    }
    lastPinActivationRef.current = { placeId, at: now };
    onSelectPlaceRef.current(placeId);
  }, []);

  const activateStay = useCallback((source: "btn" | "marker") => {
    if (!onSelectStayRef.current) return;
    const now =
      typeof performance !== "undefined" ? performance.now() : Date.now();
    if (source === "marker" && now - lastStayBtnAtRef.current < 80) {
      return;
    }
    if (source === "btn") {
      lastStayBtnAtRef.current = now;
    }
    onSelectStayRef.current();
  }, []);

  const clearDayOverlays = useCallback(() => {
    for (const entry of placeEntriesRef.current) {
      entry.btn.classList.remove("is-on");
      entry.btn.setAttribute("aria-pressed", "false");
      entry.marker?.setMap?.(null);
    }
    if (stayEntryRef.current) {
      stayEntryRef.current.btn.classList.remove("is-on");
      stayEntryRef.current.btn.setAttribute("aria-pressed", "false");
    }
    for (const overlay of extraOverlaysRef.current) {
      overlay?.setMap?.(null);
    }
    placeEntriesRef.current = [];
    stayEntryRef.current = null;
    extraOverlaysRef.current = [];
  }, []);

  const fitDayViewport = useCallback((map: any, immediately: boolean) => {
    const container = containerRef.current;
    const w = container?.clientWidth || window.innerWidth;
    const h = container?.clientHeight || Math.max(300, window.innerHeight - 52);
    const avoid = computeBookMapAvoidPadding(
      w,
      h,
      hasSidePanelRef.current,
      isReadingRef.current,
      {
        stripVisibleHeight:
          stripVisibleHeightRef.current > 0
            ? stripVisibleHeightRef.current
            : undefined,
        stripFolded: stripFoldedRef.current,
        sideWidth: sideWidthRef.current > 0 ? sideWidthRef.current : undefined,
      },
    );
    const placeMarkers = placeEntriesRef.current.map((e) => e.marker);
    const fitTargets =
      placeMarkers.length > 0 ? placeMarkers : extraOverlaysRef.current;
    if (fitTargets.length === 0) return;
    map.setFitView?.(fitTargets, immediately, avoid, 15);
  }, []);

  const renderDayOverlays = useCallback(
    (
      AMap: any,
      map: any,
      targetDay: TripDay,
      targetAccommodation: AccommodationInfo | null | undefined,
      immediately: boolean,
    ) => {
      clearDayOverlays();

      const validStops = targetDay.places.filter((p) =>
        isValidGeoPoint(p.longitude, p.latitude),
      );
      if (validStops.length > 1) {
        const path = validStops.map((p) => [p.longitude, p.latitude]);
        const routeLine = new AMap.Polyline({
          path,
          strokeColor: "#173b35",
          strokeWeight: 2,
          strokeOpacity: 0.5,
          strokeStyle: "dashed",
          strokeDasharray: [2, 7],
          lineJoin: "round",
          lineCap: "round",
          bubble: true,
          clickable: false,
          zIndex: 50,
        });
        routeLine.setMap?.(map);
        extraOverlaysRef.current.push(routeLine);
      }

      if (hasValidAccommodationCoords(targetAccommodation)) {
        const isStaySelected = Boolean(selectedStayRef.current);
        const stayClickable = Boolean(onSelectStayRef.current);
        const stayWrap = document.createElement("div");
        stayWrap.className = "gp";
        const stayBadge = document.createElement("button");
        stayBadge.type = "button";
        stayBadge.className = isStaySelected ? "gstay is-on" : "gstay";
        stayBadge.textContent = "住";
        stayBadge.setAttribute("aria-label", "查看住处详情");
        stayBadge.setAttribute("aria-pressed", String(isStaySelected));
        stayBadge.addEventListener("click", (e) => {
          e.stopPropagation();
          activateStay("btn");
        });
        const stayLabel = document.createElement("b");
        stayLabel.className = "gstay-l";
        stayLabel.textContent = "住这一带";
        stayWrap.append(stayBadge, stayLabel);

        const stayMarker = new AMap.Marker({
          position: [
            targetAccommodation.longitude,
            targetAccommodation.latitude,
          ],
          content: stayWrap,
          offset: new AMap.Pixel(0, 0),
          zIndex: isStaySelected ? 480 : 100,
          clickable: stayClickable,
          bubble: true,
        });
        stayMarker.on?.("click", (evt: any) => {
          const originTarget = evt?.originEvent?.target;
          if (
            originTarget instanceof Node &&
            stayBadge.contains(originTarget)
          ) {
            return;
          }
          activateStay("marker");
        });
        stayMarker.setMap?.(map);
        extraOverlaysRef.current.push(stayMarker);
        stayEntryRef.current = {
          marker: stayMarker,
          btn: stayBadge,
          labelEl: stayLabel,
          accommodation: targetAccommodation,
        };
      }

      const activeId = selectedPlaceIdRef.current;
      targetDay.places.forEach((place, idx) => {
        if (!isValidGeoPoint(place.longitude, place.latitude)) return;
        const isSelected = activeId !== null && place.place_id === activeId;
        const baseZIndex = 120 + idx;

        const wrap = document.createElement("div");
        wrap.className = "gp";

        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = [
          "gpin",
          place.optional ? "opt" : "",
          isSelected ? "is-on" : "",
        ]
          .filter(Boolean)
          .join(" ");
        btn.textContent = String(idx + 1);
        btn.dataset.placeId = String(place.place_id);
        btn.setAttribute(
          "aria-label",
          `第 ${idx + 1} 站 ${place.name}${place.optional ? "（可选）" : ""}`,
        );
        btn.setAttribute("aria-pressed", String(isSelected));
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          activatePin(place.place_id);
        });

        const label = document.createElement("b");
        label.className = "gpl";
        label.textContent = place.name;

        wrap.append(btn, label);

        const marker = new AMap.Marker({
          position: [place.longitude, place.latitude],
          content: wrap,
          offset: new AMap.Pixel(0, 0),
          zIndex: isSelected ? 500 : baseZIndex,
          clickable: true,
          bubble: true,
        });
        marker.on?.("click", () => {
          activatePin(place.place_id);
        });
        marker.setMap?.(map);
        placeEntriesRef.current.push({
          placeId: place.place_id,
          marker,
          btn,
          labelEl: label,
          baseZIndex,
          place,
        });
      });

      prevDayNumberRef.current = targetDay.day;
      renderedOverlaySigRef.current = buildOverlaySignature(
        targetDay,
        targetAccommodation,
      );
      fitDayViewport(map, immediately);
    },
    [activatePin, activateStay, clearDayOverlays, fitDayViewport],
  );

  // 初始化高德 JS API 2.0（严格按 MapView.tsx 方式设置安全密钥）
  useEffect(() => {
    let destroyed = false;
    const key = import.meta.env.VITE_AMAP_KEY;
    if (!key) {
      setMapFailed(true);
      return;
    }

    const security = import.meta.env.VITE_AMAP_SECURITY;
    if (security) {
      (window as any)._AMapSecurityConfig = { securityJsCode: security };
    }

    loadAmapSdk(key)
      .then((AMap) => {
        if (destroyed || !containerRef.current) return;
        const container = containerRef.current;

        let map: any;
        try {
          map = new AMap.Map(container, {
            viewMode: "2D",
            zoom: 13,
          });
        } catch {
          if (!destroyed) setMapFailed(true);
          return;
        }

        amapRef.current = AMap;
        mapRef.current = map;
        const innerMapsEl = container.querySelector<HTMLElement>(".amap-maps");
        if (innerMapsEl) {
          innerMapsEl.tabIndex = -1;
        }

        renderDayOverlays(
          AMap,
          map,
          dayRef.current,
          accommodationRef.current,
          true,
        );
        setMapReady(true);
      })
      .catch(() => {
        if (!destroyed) {
          setMapFailed(true);
        }
      });

    return () => {
      destroyed = true;
      clearDayOverlays();
      mapRef.current?.destroy?.();
      mapRef.current = null;
      amapRef.current = null;
      prevDayNumberRef.current = null;
      renderedOverlaySigRef.current = "";
      setMapReady(false);
    };
  }, [clearDayOverlays, renderDayOverlays]);

  // 当天、住宿、阅读模式或长卷可见高度变更：更新覆盖物并以对应避让边距框进视野
  useEffect(() => {
    const AMap = amapRef.current;
    const map = mapRef.current;
    if (!mapReady || !AMap || !map || mapFailed) return;

    const nextSig = buildOverlaySignature(day, accommodation);
    const overlayChanged = nextSig !== renderedOverlaySigRef.current;
    const readingChanged = prevIsReadingRef.current !== isReading;
    const foldChanged = prevStripFoldedRef.current !== stripFolded;
    const sideToggled = prevHasSidePanelRef.current !== hasSidePanel;
    const stripHeightChanged =
      !isReading &&
      !sideToggled &&
      Math.abs(stripVisibleHeight - prevStripVisibleHeightRef.current) >= 4;

    prevHasSidePanelRef.current = hasSidePanel;
    prevStripVisibleHeightRef.current = stripVisibleHeight;

    if (
      !overlayChanged &&
      !readingChanged &&
      !foldChanged &&
      !stripHeightChanged
    ) {
      return;
    }

    const supportsMoveEnd =
      typeof map.on === "function" && typeof map.off === "function";

    if (overlayChanged) {
      const prevDay = prevDayNumberRef.current;
      const daySwitched = prevDay !== null && prevDay !== day.day;
      const immediately = !daySwitched || isReducedMotionPreferred();
      if (daySwitched) {
        onCameraSettlingRef.current?.();
      }
      prevIsReadingRef.current = isReading;
      prevStripFoldedRef.current = stripFolded;
      fitSettlingUntilRef.current =
        !immediately && supportsMoveEnd
          ? Date.now() + FIT_VIEW_SETTLE_CAP_MS
          : 0;
      renderDayOverlays(AMap, map, day, accommodation, immediately);
      return;
    }

    prevIsReadingRef.current = isReading;
    prevStripFoldedRef.current = stripFolded;
    const immediately = isReducedMotionPreferred();
    if (!immediately && supportsMoveEnd) {
      onCameraSettlingRef.current?.();
    }
    fitSettlingUntilRef.current =
      !immediately && supportsMoveEnd ? Date.now() + FIT_VIEW_SETTLE_CAP_MS : 0;
    fitDayViewport(map, immediately);
  }, [
    day,
    accommodation,
    hasSidePanel,
    isReading,
    stripVisibleHeight,
    stripFolded,
    mapReady,
    mapFailed,
    fitDayViewport,
    renderDayOverlays,
  ]);

  // 高亮选中图钉 / 住处方块，在被浮层/抽屉遮挡时轻轻让进可见区，并在视野落定后通知太阳掉落
  useEffect(() => {
    const AMap = amapRef.current;
    const map = mapRef.current;
    const container = containerRef.current;
    if (!mapReady || !AMap || !map || !container || mapFailed) return;

    const reducedMotion = isReducedMotionPreferred();

    let selectedEntry: PlaceMarkerEntry | null = null;
    for (const entry of placeEntriesRef.current) {
      const isSelected =
        selectedPlaceId !== null && entry.placeId === selectedPlaceId;
      entry.btn.classList.toggle("is-on", isSelected);
      entry.btn.setAttribute("aria-pressed", String(isSelected));
      entry.marker.setzIndex?.(isSelected ? 500 : entry.baseZIndex);
      if (isSelected) {
        selectedEntry = entry;
      }
    }

    const stayEntry = stayEntryRef.current;
    if (stayEntry) {
      const isStayOn = Boolean(selectedStay);
      stayEntry.btn.classList.toggle("is-on", isStayOn);
      stayEntry.btn.setAttribute("aria-pressed", String(isStayOn));
      stayEntry.marker.setzIndex?.(isStayOn ? 480 : 100);
    }

    const performNudge = (): boolean => {
      const avoidMetrics: BookMapAvoidMetrics = {
        stripVisibleHeight:
          stripVisibleHeight > 0 ? stripVisibleHeight : undefined,
        stripFolded,
        sideWidth: sideWidth > 0 ? sideWidth : undefined,
      };
      if (selectedEntry) {
        return nudgePointIntoClearArea(
          AMap,
          map,
          selectedEntry.place.longitude,
          selectedEntry.place.latitude,
          84,
          selectedEntry.labelEl,
          container,
          hasSidePanel,
          isReading,
          reducedMotion,
          avoidMetrics,
        );
      }
      if (selectedStay && stayEntry) {
        return nudgePointIntoClearArea(
          AMap,
          map,
          stayEntry.accommodation.longitude,
          stayEntry.accommodation.latitude,
          68,
          stayEntry.labelEl,
          container,
          hasSidePanel,
          isReading,
          reducedMotion,
          avoidMetrics,
        );
      }
      return false;
    };

    const emitCameraSettled = () => {
      const cb = onCameraSettledRef.current;
      if (!cb || mapFailed) return;
      const w = container.clientWidth || window.innerWidth;
      const h =
        container.clientHeight || Math.max(300, window.innerHeight - 52);
      const avoidPoints: SunDropPoint[] = [];
      if (typeof map.lngLatToContainer === "function") {
        for (const entry of placeEntriesRef.current) {
          const pt = readContainerPx(
            map.lngLatToContainer([
              entry.place.longitude,
              entry.place.latitude,
            ]),
          );
          if (pt) avoidPoints.push(pt);
        }
        if (stayEntry) {
          const pt = readContainerPx(
            map.lngLatToContainer([
              stayEntry.accommodation.longitude,
              stayEntry.accommodation.latitude,
            ]),
          );
          if (pt) avoidPoints.push(pt);
        }
      }
      cb({
        dayNum: dayRef.current.day,
        stageWidth: w,
        stageHeight: h,
        hasSidePanel: hasSidePanelRef.current,
        isReading: isReadingRef.current,
        avoidPoints,
      });
    };

    const remainingWaitMs = fitSettlingUntilRef.current - Date.now();
    const supportsMoveEnd =
      typeof map.on === "function" && typeof map.off === "function";

    let cleanedUp = false;
    let fitTimerId: number | null = null;
    let panTimerId: number | null = null;
    let activeFitFinish: (() => void) | null = null;
    let activePanFinish: (() => void) | null = null;

    const cleanup = () => {
      cleanedUp = true;
      if (fitTimerId !== null) {
        window.clearTimeout(fitTimerId);
        fitTimerId = null;
      }
      if (panTimerId !== null) {
        window.clearTimeout(panTimerId);
        panTimerId = null;
      }
      if (activeFitFinish) {
        map.off?.("moveend", activeFitFinish);
        activeFitFinish = null;
      }
      if (activePanFinish) {
        map.off?.("moveend", activePanFinish);
        activePanFinish = null;
      }
    };

    const settleAfterNudge = () => {
      if (cleanedUp) return;
      const didAnimatedPan = performNudge();
      if (didAnimatedPan && supportsMoveEnd) {
        let panDone = false;
        const finishPan = () => {
          if (panDone || cleanedUp) return;
          panDone = true;
          if (panTimerId !== null) {
            window.clearTimeout(panTimerId);
            panTimerId = null;
          }
          map.off?.("moveend", finishPan);
          activePanFinish = null;
          emitCameraSettled();
        };
        activePanFinish = finishPan;
        map.on("moveend", finishPan);
        panTimerId = window.setTimeout(finishPan, 650);
        return;
      }
      emitCameraSettled();
    };

    if (remainingWaitMs > 0 && supportsMoveEnd) {
      let finished = false;
      const finish = () => {
        if (finished || cleanedUp) return;
        finished = true;
        fitSettlingUntilRef.current = 0;
        if (fitTimerId !== null) {
          window.clearTimeout(fitTimerId);
          fitTimerId = null;
        }
        map.off?.("moveend", finish);
        activeFitFinish = null;
        settleAfterNudge();
      };
      activeFitFinish = finish;
      map.on("moveend", finish);
      fitTimerId = window.setTimeout(finish, remainingWaitMs);
      return cleanup;
    }

    fitSettlingUntilRef.current = 0;
    settleAfterNudge();
    return cleanup;
  }, [
    selectedPlaceId,
    selectedStay,
    hasSidePanel,
    isReading,
    stripVisibleHeight,
    stripFolded,
    sideWidth,
    day,
    mapReady,
    mapFailed,
  ]);

  // 容器尺寸从 0 变为可见时重新 resize 并框进视野
  useEffect(() => {
    const stage = stageRef.current;
    if (
      !stage ||
      typeof ResizeObserver === "undefined" ||
      !mapReady ||
      mapFailed
    ) {
      return;
    }
    let lastW = stage.clientWidth;
    let lastH = stage.clientHeight;
    const ro = new ResizeObserver(() => {
      const map = mapRef.current;
      const w = stage.clientWidth;
      const h = stage.clientHeight;
      if (!map || w <= 0 || h <= 0) return;
      const wasZero = lastW === 0 || lastH === 0;
      const changed = Math.abs(w - lastW) > 2 || Math.abs(h - lastH) > 2;
      lastW = w;
      lastH = h;
      if (!changed) return;
      map.resize?.();
      if (wasZero) {
        fitDayViewport(map, true);
      }
    });
    ro.observe(stage);
    return () => ro.disconnect();
  }, [fitDayViewport, mapReady, mapFailed]);

  return (
    <div className="bk-stage" ref={stageRef}>
      {mapFailed ? (
        <div className="bk-fallback" data-testid="bk-map-fallback">
          <div className="bk-fallback-center">
            <span className="bk-fallback-note">地图暂时打不开</span>
          </div>
        </div>
      ) : (
        <>
          <div
            ref={containerRef}
            className="bk-map"
            data-testid="bk-amap-container"
          />
          {mapReady && (
            <BookWeather
              AMap={amapRef.current}
              map={mapRef.current}
              containerEl={containerRef.current}
              visual={weatherVisual}
            />
          )}
        </>
      )}
    </div>
  );
}
