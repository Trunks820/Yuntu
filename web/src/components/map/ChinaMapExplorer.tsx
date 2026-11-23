import { useState, useMemo, useRef } from "react";
import {
  CHINA_OUTLINE_PATH,
  REGIONS,
  REGION_VIEWBOXES,
  type RegionType,
  type MapCityPoint,
} from "@/constants/chinaGeo";
import { useDestinations } from "@/hooks/useDestinations";
import { destinationsToMapPoints } from "@/utils/destinationTransform";
import {
  Compass,
  ArrowRight,
  Maximize2,
  ZoomIn,
  RotateCcw,
  Loader2,
  AlertCircle,
} from "lucide-react";

interface ChinaMapExplorerProps {
  selectedCity?: string;
  onSelectCity?: (cityName: string) => void;
  className?: string;
}

export function ChinaMapExplorer({
  selectedCity = "北京",
  onSelectCity,
  className = "",
}: ChinaMapExplorerProps) {
  const [activeRegion, setActiveRegion] = useState<RegionType>("全部");
  const [hoveredCity, setHoveredCity] = useState<MapCityPoint | null>(null);
  const [currentCityName, setCurrentCityName] = useState<string>(selectedCity);
  const [isFullChina, setIsFullChina] = useState(false);

  const containerRef = useRef<HTMLDivElement>(null);

  // Fetch destinations from API
  const { data: directory, isLoading, error, refetch } = useDestinations();

  const destinations = directory?.destinations;

  // Transform API data to map points
  const mapCities = useMemo(() => {
    if (!destinations?.length) return [];
    return destinationsToMapPoints(destinations);
  }, [destinations]);

  // 计算当前动态视口（大区平滑变焦）
  const curBox = useMemo(() => {
    if (isFullChina) {
      return { x: 0, y: 0, width: 900, height: 700, title: "全国全境（960万平方公里）" };
    }
    return REGION_VIEWBOXES[activeRegion] || REGION_VIEWBOXES["全部"];
  }, [activeRegion, isFullChina]);

  // 当前激活/展示的城市
  const displayCity = useMemo(() => {
    if (hoveredCity) return hoveredCity;
    return (
      mapCities.find((c) => c.name === currentCityName) ||
      mapCities[0]
    );
  }, [hoveredCity, currentCityName, mapCities]);

  const handleCityClick = (city: MapCityPoint) => {
    setCurrentCityName(city.name);
    onSelectCity?.(city.name);
  };

  // Get city cover from API data
  const coverPhoto = useMemo(() => {
    const dest = destinations?.find(d => d.name === displayCity?.name);
    return dest?.coverImageUrl || "/city-placeholder.svg";
  }, [displayCity?.name, destinations]);

  // 变焦比例系数（用于动态调节点位与文字大小，防止过度放大或缩得过小）
  const isZoomed = curBox.width < 250;
  const pinRadius = isFullChina ? 6 : isZoomed ? 4.5 : 5.5;
  const textFontSize = isFullChina ? 9 : isZoomed ? 6.5 : 8.5;

  // Loading state
  if (isLoading) {
    return (
      <div className={`relative flex items-center justify-center rounded-3xl border border-sand-200/90 bg-gradient-to-b from-[#fdfbf7] to-[#f7f3ea] p-8 shadow-card ${className}`}>
        <div className="flex flex-col items-center gap-3">
          <Loader2 className="h-8 w-8 animate-spin text-emerald-600" />
          <p className="text-sm text-gray-600">加载地图数据中...</p>
        </div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className={`relative flex items-center justify-center rounded-3xl border border-red-200/90 bg-gradient-to-b from-[#fdfbf7] to-[#f7f3ea] p-8 shadow-card ${className}`}>
        <div className="flex flex-col items-center gap-3">
          <AlertCircle className="h-8 w-8 text-red-500" />
          <p className="text-sm text-red-600">地图数据加载失败</p>
          <p className="text-xs text-gray-500">{error.message}</p><button type="button" onClick={() => void refetch()}>重试</button>
        </div>
      </div>
    );
  }

  // Empty state
  if (!mapCities.length) {
    return (
      <div className={`relative flex items-center justify-center rounded-3xl border border-sand-200/90 bg-gradient-to-b from-[#fdfbf7] to-[#f7f3ea] p-8 shadow-card ${className}`}>
        <p className="text-sm text-gray-600">暂无城市数据</p>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className={`relative flex flex-col rounded-3xl border border-sand-200/90 bg-gradient-to-b from-[#fdfbf7] to-[#f7f3ea] p-3 sm:p-5 shadow-card overflow-hidden ${className}`}
    >
      {/* 1. 顶部控制栏：大区变焦 Pills + 视角模式切换 */}
      <div className="z-10 flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 pb-2.5 border-b border-sand-200/70">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-xl bg-emerald-100 text-emerald-800 text-xs shadow-2xs">
            <Compass size={14} />
          </span>
          <div>
            <h3 className="font-display text-sm font-bold text-gray-900 tracking-tight flex items-center gap-1.5">
              <span>智能变焦地图</span>
              <span className="rounded-full bg-emerald-100/80 px-2 py-0.5 text-[10px] font-semibold text-emerald-800 border border-emerald-200">
                {curBox.title}
              </span>
            </h3>
          </div>
        </div>

        {/* 区域变焦选项卡 */}
        <div className="flex items-center gap-1 overflow-x-auto hide-scrollbar py-0.5">
          {REGIONS.map((region) => {
            const count =
              region === "全部"
                ? mapCities.length
                : mapCities.filter((c) => c.region === region).length;
            const active = !isFullChina && activeRegion === region;
            return (
              <button
                key={region}
                type="button"
                onClick={() => {
                  setIsFullChina(false);
                  setActiveRegion(region);
                }}
                className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium transition-all ${
                  active
                    ? "bg-emerald-700 text-white shadow-xs font-bold ring-2 ring-emerald-600/30"
                    : "bg-white/80 text-gray-600 hover:bg-white hover:text-gray-900 border border-sand-200/80"
                }`}
              >
                {region} {count > 0 && `(${count})`}
              </button>
            );
          })}

          {/* 全境对比开关 */}
          <button
            type="button"
            onClick={() => setIsFullChina(!isFullChina)}
            title={isFullChina ? "切换回活跃名城聚焦" : "查看全国全境"}
            className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium transition-all flex items-center gap-1 ${
              isFullChina
                ? "bg-gray-800 text-white shadow-xs font-bold"
                : "bg-sand-200/70 text-gray-600 hover:bg-sand-300/80 hover:text-gray-900"
            }`}
          >
            <Maximize2 size={10} />
            <span>{isFullChina ? "聚焦核心带" : "全境"}</span>
          </button>
        </div>
      </div>

      {/* 2. 主体区：100% 满宽变焦 SVG 地图画布（右侧不再强占 300px） */}
      <div className="relative w-full h-[400px] sm:h-[480px] flex items-center justify-center pt-2 overflow-hidden rounded-2xl">
        {/* 背景经纬虚线网格 */}
        <div className="absolute inset-0 pointer-events-none opacity-40">
          <div className="h-full w-full bg-[radial-gradient(#d8ceba_1px,transparent_1px)] [background-size:20px_20px]" />
        </div>

        {/* 变焦 SVG 画布 */}
        <svg
          viewBox={`${curBox.x} ${curBox.y} ${curBox.width} ${curBox.height}`}
          className="w-full h-full drop-shadow-sm select-none transition-all duration-700 ease-out"
          preserveAspectRatio="xMidYMid meet"
          aria-label="动态变焦探索地图"
        >
          {/* 经纬度参考线 */}
          <g stroke="#eae3d4" strokeDasharray="3 4" strokeWidth={isZoomed ? 0.4 : 0.8} opacity="0.6">
            <line x1="20" y1="200" x2="880" y2="200" />
            <line x1="20" y1="350" x2="880" y2="350" />
            <line x1="20" y1="500" x2="880" y2="500" />
            <line x1="300" y1="20" x2="300" y2="680" />
            <line x1="500" y1="20" x2="500" y2="680" />
            <line x1="700" y1="20" x2="700" y2="680" />
          </g>

          {/* 中国大陆陆地与岛屿轮廓 */}
          <path
            d={CHINA_OUTLINE_PATH}
            fill="#f5efe4"
            stroke="#d5c8b2"
            strokeWidth={isZoomed ? 0.6 : 1.2}
            strokeLinejoin="round"
            strokeLinecap="round"
            className="transition-all duration-500"
          />

          {/* 南海诸岛插图 (在全境模式或华南变焦时展示) */}
          {isFullChina && (
            <g transform="translate(730, 520)" className="text-[10px] fill-gray-400">
              <rect
                width="140"
                height="150"
                fill="#fcf9f2"
                stroke="#dfd5c2"
                strokeWidth="0.8"
                rx="8"
                opacity="0.9"
              />
              <text x="70" y="140" textAnchor="middle" fontSize="9" fill="#9ca3af">
                南海诸岛
              </text>
            </g>
          )}

          {/* 16 座名城点位（应用防碰撞标签位移 + 变焦独立层） */}
          {mapCities.map((city) => {
            const isSelected = currentCityName === city.name;
            const isHovered = hoveredCity?.name === city.name;
            const isDimmed =
              !isFullChina &&
              activeRegion !== "全部" &&
              city.region !== activeRegion;

            // 防重叠位移策略
            const label = destinations?.find(d => d.name === city.name)?.mapLabelOffset;
            const offset = label ? { dx: label.x, dy: label.y, align: "middle" as const } : {
              dx: 8,
              dy: 0,
              align: "start",
            };
            const labelDx = isZoomed ? offset.dx * 1.3 : offset.dx;
            const labelDy = isZoomed ? offset.dy * 1.3 : offset.dy;

            return (
              <g
                key={city.name}
                transform={`translate(${city.x}, ${city.y})`}
                className={`cursor-pointer transition-all duration-300 ${
                  isDimmed ? "opacity-20 pointer-events-none" : "opacity-100"
                }`}
                onClick={() => handleCityClick(city)}
                onMouseEnter={() => setHoveredCity(city)}
                onMouseLeave={() => setHoveredCity(null)}
              >
                {/* 选中态波纹扩散动画 */}
                {(isSelected || isHovered) && (
                  <circle
                    r={pinRadius * 2.2}
                    fill="#059669"
                    opacity="0.3"
                    className="animate-ping"
                  />
                )}

                {/* 外圈光晕环 */}
                <circle
                  r={isSelected || isHovered ? pinRadius * 1.4 : pinRadius}
                  fill={isSelected ? "#059669" : "#10b981"}
                  stroke="#ffffff"
                  strokeWidth={isZoomed ? "1" : "1.8"}
                  className="transition-all duration-200 drop-shadow-sm"
                />

                {/* 核心中心白点 */}
                <circle r={pinRadius * 0.4} fill="#ffffff" />

                {/* 防碰撞城市名标签胶囊 */}
                <g transform={`translate(${labelDx}, ${labelDy})`}>
                  {/* 背景小药丸 */}
                  <rect
                    x={
                      offset.align === "end"
                        ? -(city.name.length * textFontSize + 10)
                        : offset.align === "middle"
                        ? -(city.name.length * textFontSize * 0.5 + 5)
                        : -2
                    }
                    y={-(textFontSize + 3)}
                    width={city.name.length * textFontSize + 10}
                    height={textFontSize + 7}
                    rx={isZoomed ? 3 : 5}
                    fill={isSelected ? "#064e3b" : "rgba(255, 255, 255, 0.94)"}
                    stroke={isSelected ? "#059669" : "#d8cfbd"}
                    strokeWidth={isZoomed ? "0.5" : "0.8"}
                    className="transition-colors shadow-2xs"
                  />
                  <text
                    x={
                      offset.align === "end"
                        ? -(city.name.length * textFontSize * 0.5 + 5)
                        : offset.align === "middle"
                        ? 0
                        : city.name.length * textFontSize * 0.5 + 3
                    }
                    y="-1"
                    textAnchor="middle"
                    fontSize={textFontSize}
                    fontWeight={isSelected ? "bold" : "600"}
                    fill={isSelected ? "#ffffff" : "#1f2937"}
                    className="select-none tracking-tight pointer-events-none font-sans"
                  >
                    {city.name}
                  </text>
                </g>
              </g>
            );
          })}
        </svg>

        {/* 3. 右下角轻量悬浮预览微卡（Floating Card，不占主版面） */}
        <div className="absolute bottom-2.5 right-2.5 z-30 max-w-[260px] sm:max-w-xs rounded-2xl border border-sand-300 bg-white/95 p-2.5 sm:p-3 shadow-xl backdrop-blur-md transition-all">
          <div className="flex items-center gap-2.5">
            {/* 缩略图 */}
            <div className="relative h-14 w-14 sm:h-16 sm:w-16 shrink-0 overflow-hidden rounded-xl bg-sand-100">
              <img
                src={coverPhoto}
                alt={displayCity?.name || "城市"}
                className="h-full w-full object-cover"
                onError={(event) => {
                  event.currentTarget.onerror = null;
                  event.currentTarget.src = "/city-placeholder.svg";
                }}
              />
              <span className="absolute bottom-0.5 right-0.5 rounded bg-black/60 px-1 py-0.2 font-mono text-[8px] font-bold text-white">
                {displayCity?.iata || "N/A"}
              </span>
            </div>

            {/* 文字与选定 */}
            <div className="flex-1 min-w-0 text-left">
              <div className="flex items-baseline justify-between gap-1">
                <span className="font-display font-black text-sm text-gray-900 truncate">
                  {displayCity?.name || "未知"}
                </span>
                <span className="text-[10px] text-emerald-700 font-semibold bg-emerald-50 px-1.5 py-0.5 rounded">
                  {displayCity?.region || "未知"}
                </span>
              </div>
              <p className="text-[10px] font-medium text-emerald-800 truncate mt-0.5">
                {displayCity?.tag || "探索这座城市"}
              </p>
              <button
                type="button"
                onClick={() => displayCity && handleCityClick(displayCity)}
                className="mt-1 inline-flex items-center gap-1 text-[11px] font-bold text-emerald-700 hover:text-emerald-800 transition-colors"
              >
                <span>{currentCityName === displayCity?.name ? "已选定" : "点击选定该城市"}</span>
                <ArrowRight size={10} />
              </button>
            </div>
          </div>
        </div>

        {/* 4. 左下角快捷变焦复位工具栏 */}
        <div className="absolute bottom-2.5 left-2.5 z-20 flex items-center gap-1 bg-white/85 backdrop-blur-md rounded-xl p-1 border border-sand-200 shadow-xs">
          <button
            type="button"
            onClick={() => {
              setIsFullChina(false);
              setActiveRegion("全部");
            }}
            title="复位至核心名城带"
            className="flex items-center gap-1 px-2 py-1 text-[10px] font-bold text-gray-700 hover:bg-sand-100 rounded-lg transition-colors"
          >
            <RotateCcw size={10} />
            <span>复位</span>
          </button>
          <span className="h-3 w-px bg-sand-200" />
          <button
            type="button"
            onClick={() => {
              // 循环大区快速演示变焦
              const regionsWithoutAll: readonly RegionType[] = REGIONS.filter(
                (r) => r !== "全部"
              );
              const currentIndex = regionsWithoutAll.indexOf(activeRegion);
              const nextIndex =
                currentIndex === -1
                  ? 0
                  : (currentIndex + 1) % regionsWithoutAll.length;
              setIsFullChina(false);
              setActiveRegion(regionsWithoutAll[nextIndex]);
            }}
            title="切换下一个大区特写"
            className="flex items-center gap-1 px-2 py-1 text-[10px] font-bold text-emerald-700 hover:bg-emerald-50 rounded-lg transition-colors"
          >
            <ZoomIn size={10} />
            <span>大区变焦 →</span>
          </button>
        </div>
      </div>

      {/* 5. 底部图例说明 */}
      <div className="z-10 mt-2.5 pt-2 border-t border-sand-200/70 flex flex-wrap items-center justify-between text-[11px] text-gray-500 gap-2">
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full bg-emerald-500 ring-2 ring-emerald-200" />
            <span>点选城市光点即可锁定</span>
          </span>
          <span className="text-gray-400">
            * 点顶部大区直接自动变焦放大对应片区，长三角永不重叠
          </span>
        </div>
        <span className="text-emerald-700 font-semibold text-[11px]">
          ✓ 100% 满屏自适应
        </span>
      </div>
    </div>
  );
}
