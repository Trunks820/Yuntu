/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from "react";
import AMapLoader from "@amap/amap-jsapi-loader";
import type { TripDay, AccommodationInfo } from "@/types/trip";
import { decodePolyline } from "@/utils/polyline";
import { isAnchorRole } from "@/constants/places";
import { getDayPalette } from "@/constants/mapPalette";
import { MapPinned } from "lucide-react";

interface MapViewProps {
  day?: TripDay;
  days?: TripDay[];
  selectedDay?: number | "all";
  accommodation?: AccommodationInfo | null;
  activePlaceId?: number | null;
  onMarkerClick?: (placeId: number) => void;
}

// 与全局色板一致（src/index.css 的 --yt-*）：旧绿旧橙已收敛
const MODE_COLOR: Record<string, string> = {
  walking: "#143c35",
  transit: "#166c5b",
  taxi: "#a35b48",
  driving: "#166c5b",
};

export function MapView({
  day,
  days,
  selectedDay,
  accommodation,
  activePlaceId,
  onMarkerClick,
}: MapViewProps) {
  const mapRef = useRef<any>(null);
  const amapRef = useRef<any>(null);
  const satelliteRef = useRef<any>(null);
  const markersRef = useRef<any[]>([]);
  const polylinesRef = useRef<any[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<"standard" | "satellite">("standard");
  const containerRef = useRef<HTMLDivElement>(null);

  const isAllDays = selectedDay === "all" || (!day && Boolean(days && days.length > 0));
  const renderDays: TripDay[] = isAllDays && days && days.length > 0
    ? days
    : (day ? [day] : (days && days.length > 0 ? [days[0]] : []));

  function toggleView(next: "standard" | "satellite") {
    const AMap = amapRef.current;
    const map = mapRef.current;
    if (!AMap || !map || next === view) return;
    if (next === "satellite") {
      if (!satelliteRef.current) satelliteRef.current = new AMap.TileLayer.Satellite();
      map.add(satelliteRef.current);
    } else {
      if (satelliteRef.current) {
        map.remove(satelliteRef.current);
      }
    }
    setView(next);
  }

  useEffect(() => {
    let destroyed = false;

    const key = import.meta.env.VITE_AMAP_KEY;
    if (!key) {
      setError(true);
      return;
    }

    // 新版高德 Web JS API 需要安全密钥配对（本地明文，上线改后端代理）
    const security = import.meta.env.VITE_AMAP_SECURITY;
    if (security) {
      (window as any)._AMapSecurityConfig = { securityJsCode: security };
    }

    AMapLoader.load({ key, version: "2.0" })
      .then((AMap) => {
        if (destroyed || !containerRef.current) return;

        amapRef.current = AMap;
        const map = new AMap.Map(containerRef.current, {
          zoom: 13,
          viewMode: "2D",
        });
        mapRef.current = map;

        updateMarkers(AMap, map, renderDays, isAllDays, accommodation, activePlaceId, onMarkerClick, markersRef);
        updatePolylines(AMap, map, renderDays, isAllDays, polylinesRef);
        fitView(map);
        setLoading(false);
      })
      .catch(() => {
        if (!destroyed) {
          setError(true);
          setLoading(false);
        }
      });

    return () => {
      destroyed = true;
      mapRef.current?.destroy();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // renderDays/accommodation 变化：重建全部覆盖物并 fitView 全览
  useEffect(() => {
    const AMap = amapRef.current;
    const map = mapRef.current;
    if (!AMap || !map) return;

    clearOverlays(markersRef, polylinesRef);
    updateMarkers(AMap, map, renderDays, isAllDays, accommodation, activePlaceId, onMarkerClick, markersRef);
    updatePolylines(AMap, map, renderDays, isAllDays, polylinesRef);
    fitView(map);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDay, day, days, accommodation]);

  // activePlaceId 变化：重建 marker 更新高亮，并平移到选中点（不重置缩放）
  useEffect(() => {
    const AMap = amapRef.current;
    const map = mapRef.current;
    if (!AMap || !map) return;

    clearOverlays(markersRef, polylinesRef);
    updateMarkers(AMap, map, renderDays, isAllDays, accommodation, activePlaceId, onMarkerClick, markersRef);
    updatePolylines(AMap, map, renderDays, isAllDays, polylinesRef);

    let active: { longitude?: number; latitude?: number } | undefined;
    for (const d of renderDays) {
      const found = d.places.find((p) => p.place_id === activePlaceId);
      if (found) {
        active = found;
        break;
      }
    }
    if (active?.longitude && active?.latitude) {
      map.panTo([active.longitude, active.latitude], 250);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePlaceId]);

  // 容器尺寸变化（移动端 Tab 切换：地图从 hidden(0尺寸) 变可见）时，
  // 高德地图需 resize + 重新 fitView，否则会停在 0 尺寸初始化时的错误缩放（缩太远）
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let prevW = el.clientWidth;
    const ro = new ResizeObserver(() => {
      const map = mapRef.current;
      const w = el.clientWidth;
      // 仅在从不可见(0)变为可见时重排，避免正常缩放/拖动被打断
      if (map && prevW === 0 && w > 0) {
        map.resize();
        fitView(map);
      }
      prevW = w;
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (error)
    return (
      <div className="card flex h-full w-full flex-col items-center justify-center gap-3 bg-primary-50/30 p-6 text-center">
        <MapPinned size={30} className="text-primary-200" aria-hidden="true" />
        <p className="text-sm font-medium text-gray-600">地图加载失败</p>
        <p className="text-xs text-gray-400">行程信息不受影响，可刷新页面重试</p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-1 rounded-lg border border-primary-200 px-4 py-1.5 text-sm font-medium text-primary-700 transition-colors hover:bg-primary-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-300"
        >
          刷新重试
        </button>
      </div>
    );

  return (
    <div className="card relative h-full w-full overflow-hidden">
      {loading && (
        <div className="absolute inset-0 flex items-center justify-center bg-primary-50/50">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary-200 border-t-primary-500" />
        </div>
      )}
      <div ref={containerRef} className="h-full w-full" />

      {/* 图层切换 */}
      <div className="absolute right-2 top-2 flex overflow-hidden rounded-lg border border-gray-100 bg-white shadow-sm">
        <button
          type="button"
          onClick={() => toggleView("standard")}
          className={`px-3 py-1.5 text-xs font-medium transition-colors ${
            view === "standard" ? "bg-primary-50 text-primary-600" : "bg-white text-gray-600 hover:bg-gray-50"
          }`}
        >
          地图
        </button>
        <button
          type="button"
          onClick={() => toggleView("satellite")}
          className={`border-l border-gray-100 px-3 py-1.5 text-xs font-medium transition-colors ${
            view === "satellite" ? "bg-primary-50 text-primary-600" : "bg-white text-gray-600 hover:bg-gray-50"
          }`}
        >
          卫星
        </button>
      </div>

      {/* 全程总览状态指示 - 提升到 bottom-7 避开高德左下角版权水印 */}
      {isAllDays && renderDays.length > 0 && (
        <div className="absolute bottom-7 left-3 z-[60] flex items-center gap-1.5 rounded-xl bg-gray-900/85 px-3 py-1.5 text-[11px] font-medium text-white shadow-lg backdrop-blur-md border border-white/10">
          <span className="flex h-2 w-2 rounded-full bg-emerald-400 animate-pulse shadow-sm" />
          <span>全程总览：共 {renderDays.length} 天 · {renderDays.reduce((acc, d) => acc + d.places.length, 0)} 个地点</span>
        </div>
      )}

      <div className="absolute bottom-7 right-3 z-[60] rounded-xl bg-white/90 px-3 py-1 text-[11px] font-medium text-sand-600 shadow-md backdrop-blur-md border border-gray-100">
        {isAllDays ? "多日全景轨迹" : "路线顺序示意图"}
      </div>
    </div>
  );
}

function updateMarkers(
  AMap: any,
  map: any,
  renderDays: TripDay[],
  isAllDays: boolean,
  accommodation: AccommodationInfo | null | undefined,
  activePlaceId: number | null | undefined,
  onMarkerClick: ((id: number) => void) | undefined,
  markersRef: React.MutableRefObject<any[]>
) {
  // 如果有住宿建议且坐标有效，绘制住宿锚点 Marker
  if (accommodation && accommodation.longitude && accommodation.latitude) {
    const isUserSpecified = accommodation.source === "user_specified";
    const bg = isUserSpecified ? "#0284c7" : "#0d9488";
    const dot = 26;
    const ring = 2;
    const nameBg = isUserSpecified ? "#f0f9ff" : "#f0fdf4";
    const nameBorder = isUserSpecified ? "#7dd3fc" : "#86efac";
    const nameColor = isUserSpecified ? "#0369a1" : "#15803d";
    const tagText = isUserSpecified ? "住宿" : "推荐住";

    const accMarker = new AMap.Marker({
      position: [accommodation.longitude, accommodation.latitude],
      zIndex: 150,
      content: `
        <div style="display:flex;align-items:center;gap:4px;transform:translate(-${dot / 2}px,-${dot / 2}px);white-space:nowrap;cursor:pointer">
          <div style="width:${dot}px;height:${dot}px;border-radius:50%;background:${bg};color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;box-shadow:0 2px 8px rgba(0,0,0,.25);border:${ring}px solid #fff">🏨</div>
          <div style="font-size:11.5px;font-weight:700;color:${nameColor};background:${nameBg};border:1px solid ${nameBorder};border-radius:7px;padding:2px 6.5px;box-shadow:0 2px 8px rgba(0,0,0,.15);backdrop-filter:blur(4px)">
            <span style="font-size:10px;opacity:0.85;margin-right:3px;">[${tagText}]</span>${accommodation.name}
          </div>
        </div>`,
      offset: new AMap.Pixel(0, 0),
    });

    accMarker.on("mouseover", () => accMarker.setzIndex(9999));
    accMarker.on("mouseout", () => accMarker.setzIndex(150));

    map.add(accMarker);
    markersRef.current.push(accMarker);
  }

  // 记录已放置的 Marker 坐标与垂直位移，实现临近点智能错位（防压字遮挡）
  const placedPins: { lng: number; lat: number; yShift: number }[] = [];

  renderDays.forEach((currentDay) => {
    const palette = getDayPalette(currentDay.day);

    currentDay.places.forEach((place, i) => {
      if (!place.longitude || !place.latitude) return;

      const isActive = place.place_id === activePlaceId;
      const isAnchor = isAnchorRole(place.role);
      const label = isAllDays ? `D${currentDay.day}-${i + 1}` : `${i + 1}`;

      // 计算与已有标记点的经纬度距离，如果在屏幕/经纬度尺度上极度接近（< 0.012度，约1km内），进行上下智能错位
      let yShift = 0;
      for (const p of placedPins) {
        const dLng = Math.abs(place.longitude - p.lng);
        const dLat = Math.abs(place.latitude - p.lat);
        if (dLng < 0.012 && dLat < 0.010) {
          yShift = p.yShift <= 0 ? 24 : -24;
          break;
        }
      }
      placedPins.push({ lng: place.longitude, lat: place.latitude, yShift });

      const bg = isActive
        ? (isAllDays ? palette.dark : "#143c35")
        : (isAllDays ? palette.primary : (isAnchor ? "#166c5b" : "#b57e6b"));

      const dotWidth = isAllDays ? (label.length > 4 ? 40 : 34) : (isActive ? 30 : 24);
      const dotHeight = isAllDays ? 22 : (isActive ? 30 : 24);
      const borderRadius = isAllDays ? "11px" : "50%";
      const ring = isActive ? 3 : 2;
      const nameWeight = isActive ? 700 : 600;
      const nameColor = isActive ? (isAllDays ? palette.dark : "#0f2f2a") : "#374151";
      const nameBg = isActive ? (isAllDays ? palette.lightBg : "#eef9f7") : "rgba(255,255,255,.96)";
      const nameBorder = isActive ? (isAllDays ? palette.border : "#c4dbcb") : "#e5e7eb";
      const baseZIndex = isActive ? 500 : (100 + currentDay.day * 20 + i);

      const marker = new AMap.Marker({
        position: [place.longitude, place.latitude],
        // 选中项提到最上层，避免被其它 marker 名称遮住
        zIndex: baseZIndex,
        content: `
          <div style="display:flex;align-items:center;gap:4px;transform:translate(-${dotWidth / 2}px,calc(-${dotHeight / 2}px + ${yShift}px));white-space:nowrap;cursor:pointer">
            <div style="min-width:${dotWidth}px;height:${dotHeight}px;padding:0 4px;border-radius:${borderRadius};background:${bg};color:#fff;display:flex;align-items:center;justify-content:center;font-size:${isAllDays ? 11 : (isActive ? 14 : 12)}px;font-weight:700;box-shadow:0 2px 8px rgba(0,0,0,.25);border:${ring}px solid #fff;letter-spacing:-0.2px">${label}</div>
            <div style="font-size:11.5px;font-weight:${nameWeight};color:${nameColor};background:${nameBg};border:1px solid ${nameBorder};border-radius:7px;padding:2px 6.5px;box-shadow:0 2px 8px rgba(0,0,0,.15);backdrop-filter:blur(4px);max-width:130px;overflow:hidden;text-overflow:ellipsis">${place.name}</div>
          </div>`,
        offset: new AMap.Pixel(0, 0),
      });

      marker.on("mouseover", () => marker.setzIndex(9999));
      marker.on("mouseout", () => marker.setzIndex(baseZIndex));

      marker.on("click", () => onMarkerClick?.(place.place_id));
      map.add(marker);
      markersRef.current.push(marker);
    });
  });
}

function updatePolylines(
  AMap: any,
  map: any,
  renderDays: TripDay[],
  isAllDays: boolean,
  polylinesRef: React.MutableRefObject<any[]>
) {
  renderDays.forEach((currentDay) => {
    const palette = getDayPalette(currentDay.day);

    currentDay.commute_legs.forEach((leg) => {
      const from = currentDay.places.find((p) => p.place_id === leg.from_place_id);
      const to = currentDay.places.find((p) => p.place_id === leg.to_place_id);
      if (!from?.longitude || !to?.longitude) return;

      let path: [number, number][];
      let hasRealRoute = false;

      if (leg.encoded_polyline) {
        try {
          const decoded = decodePolyline(leg.encoded_polyline);
          if (decoded.length >= 2) {
            path = decoded;
            hasRealRoute = true;
          } else {
            path = [
              [from.longitude, from.latitude],
              [to.longitude, to.latitude],
            ];
          }
        } catch {
          path = [
            [from.longitude, from.latitude],
            [to.longitude, to.latitude],
          ];
        }
      } else {
        path = [
          [from.longitude, from.latitude],
          [to.longitude, to.latitude],
        ];
      }

      const color = isAllDays ? palette.primary : (MODE_COLOR[leg.mode] ?? "#94a3b8");

      // 底层：白色描边线（比主线更粗），让彩色路线在地图底图上跳得出来——竞品(高德)的关键手法
      const outline = new AMap.Polyline({
        path,
        strokeColor: "#ffffff",
        strokeWeight: 9,
        strokeOpacity: 0.9,
        lineJoin: "round",
        lineCap: "round",
        zIndex: 50,
      });
      map.add(outline);
      polylinesRef.current.push(outline);

      // 上层：彩色实线 + 方向箭头。无真实路网(直线段)用虚线区分"非实际路径"，但仍加粗醒目
      const polyline = new AMap.Polyline({
        path,
        strokeColor: color,
        strokeWeight: 6,
        strokeStyle: hasRealRoute ? "solid" : "dashed",
        strokeOpacity: 0.95,
        strokeDasharray: hasRealRoute ? undefined : [12, 6],
        showDir: true, // 行进方向箭头，一眼看出走向
        lineJoin: "round",
        lineCap: "round",
        zIndex: 51 + currentDay.day,
      });

      map.add(polyline);
      polylinesRef.current.push(polyline);
    });
  });
}

function clearOverlays(markersRef: React.MutableRefObject<any[]>, polylinesRef: React.MutableRefObject<any[]>) {
  markersRef.current.forEach((m) => m.setMap(null));
  polylinesRef.current.forEach((p) => p.setMap(null));
  markersRef.current = [];
  polylinesRef.current = [];
}

function fitView(map: any) {
  setTimeout(() => {
    map.setFitView(null, false, [40, 40, 40, 40], 300);
  }, 100);
}
