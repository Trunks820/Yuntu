import type { AccommodationInfo } from "@/types/trip";
import { Hotel, ChevronRight } from "lucide-react";

interface AccommodationTimelineNodeProps {
  accommodation?: AccommodationInfo | null;
  day: number;
  onLocationClick?: (lat: number, lng: number) => void;
}

export function AccommodationTimelineNode({
  accommodation,
  day,
  onLocationClick,
}: AccommodationTimelineNodeProps) {
  if (!accommodation || !accommodation.name) {
    return null;
  }

  const isUserSpecified = accommodation.source === "user_specified";
  const isDay1 = day === 1;
  const name = accommodation.name;

  // 文案去重
  let reason: string | null = null;
  if (
    accommodation.reason &&
    !accommodation.reason.includes("步行可达多数主景点") &&
    accommodation.reason !== name
  ) {
    reason = accommodation.reason;
  }

  const handleClick = () => {
    if (accommodation.latitude && accommodation.longitude) {
      onLocationClick?.(accommodation.latitude, accommodation.longitude);
    }
  };

  // Day 1：像素级对齐的【第 00 站 · 行程大本营】节点
  if (isDay1) {
    return (
      <div className="relative reveal-up">
        {/* 时间轴竖连线：严格与下方景点连线对齐 (left-6 轴线) */}
        <div
          className="absolute left-6 top-12 bottom-0 w-0.5 bg-gradient-to-b from-primary-300 via-primary-200 to-gray-200"
          aria-hidden="true"
        />

        {/* 主节点框 */}
        <div
          onClick={handleClick}
          className={`group relative flex flex-col rounded-2xl border transition-all duration-200 p-5 cursor-pointer ${
            isUserSpecified
              ? "border-sky-200/90 bg-gradient-to-br from-sky-50/60 via-indigo-50/30 to-white hover:border-sky-400 hover:shadow-md"
              : "border-teal-200/90 bg-gradient-to-br from-teal-50/60 via-emerald-50/30 to-white hover:border-teal-400 hover:shadow-md"
          }`}
        >
          {/* 头部：序号图标与景点完全对齐 + 标题 + 标签 */}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              {/* 00 站图标：与下方的 01/02 景点数字徽章 1:1 像素级尺寸与排版完全一致 (h-9 w-9) */}
              <span
                className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-white font-display text-sm font-bold shadow-xs ${
                  isUserSpecified
                    ? "bg-gradient-to-br from-sky-600 to-indigo-600"
                    : "bg-gradient-to-br from-teal-600 to-emerald-600"
                }`}
              >
                🏨
              </span>

              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h3 className="truncate text-lg font-bold text-gray-900 transition-colors group-hover:text-primary-700">
                    {isUserSpecified ? `从你住的 ${name} 出发` : `建议住在 ${name} 附近`}
                  </h3>
                  <span
                    className={`shrink-0 rounded-md border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${
                      isUserSpecified
                        ? "border-sky-200 bg-sky-100/80 text-sky-700"
                        : "border-teal-200 bg-teal-100/80 text-teal-700"
                    }`}
                  >
                    {isUserSpecified ? "用户指定" : "推荐商圈"}
                  </span>
                </div>
                <span className="text-xs text-gray-400">
                  {isUserSpecified ? "行程指定的住宿地点" : "出行便捷·建议住宿大本营"}
                </span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <span className="flex items-center gap-1 rounded-lg bg-white border border-gray-100 px-2.5 py-1 text-xs font-semibold text-primary-700 shadow-2xs">
                <Hotel size={14} className="text-primary-500" />
                {isUserSpecified ? "入住大本营" : "住宿推荐地"}
              </span>
              <span className="ml-1 text-xs font-medium text-primary-600 opacity-0 group-hover:opacity-100 transition-opacity hidden sm:inline-flex items-center gap-0.5">
                地图定位 <ChevronRight size={10} />
              </span>
            </div>
          </div>

          {/* 详情与说明描述：推荐住处只保留理由；无理由时整块不渲染 */}
          {(isUserSpecified || reason) && (
            <div className="mt-3 border-t border-gray-100/80 pt-3 text-sm leading-relaxed font-medium text-gray-700">
              {isUserSpecified && (
                <p>
                  本行程路线已围绕你的入住点【{name}】进行交通与路线最优化排布。
                </p>
              )}
              {reason && (
                <p className="mt-1 text-xs text-gray-500 font-normal">
                  💡 推荐理由：{reason}
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  // Day 2及以后：像素级完全对齐的【当日起点】线索节点
  return (
    <div className="relative reveal-up">
      {/* 竖向轴线 */}
      <div
        className="absolute left-6 top-12 bottom-0 w-0.5 bg-gradient-to-b from-primary-200 via-gray-200 to-gray-200"
        aria-hidden="true"
      />

      <div
        onClick={handleClick}
        className="group relative flex items-center justify-between rounded-2xl border border-gray-200/80 bg-white p-4 transition-all duration-200 hover:border-primary-200 hover:shadow-sm cursor-pointer"
      >
        <div className="flex min-w-0 items-center gap-3">
          <span
            className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-white font-display text-sm font-bold shadow-xs ${
              isUserSpecified
                ? "bg-gradient-to-br from-sky-600 to-indigo-600"
                : "bg-gradient-to-br from-teal-600 to-emerald-600"
            }`}
          >
            🏨
          </span>

          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h4 className="truncate text-base font-bold text-gray-900 group-hover:text-primary-700 transition-colors">
                当日起点：{name}
              </h4>
              <span className="shrink-0 rounded-md bg-gray-100 border border-gray-200/60 px-1.5 py-0.5 text-[10px] font-medium text-gray-600">
                大本营出发
              </span>
            </div>
            <span className="text-xs text-gray-400">
              {isUserSpecified ? "从你的住宿点出发开始今日行程" : "从建议大本营出发开始今日行程"}
            </span>
          </div>
        </div>

        <span className="ml-2 text-xs text-primary-600 opacity-0 group-hover:opacity-100 transition-opacity hidden sm:inline-flex items-center gap-0.5">
          地图定位 <ChevronRight size={10} />
        </span>
      </div>
    </div>
  );
}
