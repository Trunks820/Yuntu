import type { TripDay, TripPlace, AccommodationInfo, TripMustInclude } from "@/types/trip";
import { AccommodationTimelineNode } from "@/components/detail/AccommodationCard";
import {
  formatDistance,
  formatMinutes,
  commuteModeName,
  commuteModeIcon,
  cleanBrief,
} from "@/utils/format";
import { categoryIcon, categoryName, isAnchorRole } from "@/constants/places";
import {
  MapPin,
  Feather,
  ChevronDown,
  ChevronRight,
  Route,
  Clock,
  ArrowRight,
  CornerUpRight,
  Navigation,
} from "lucide-react";

const PERIOD_LABEL: Record<string, string> = {
  morning: "上午",
  afternoon: "下午",
  evening: "傍晚",
  night: "夜间",
};

function formatStayDuration(minutes?: number | null): string {
  if (!minutes || minutes <= 0) return "自由漫游";
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    return mins > 0 ? `建议 ${hours}小时${mins}分` : `建议 ${hours}小时`;
  }
  return `建议 ${minutes}分钟`;
}

function placeTimeLabel(place: TripPlace): string | null {
  const s = place.schedule;
  if (!s) return null;
  if (s.exact_start) {
    return s.exact_end ? `${s.exact_start} – ${s.exact_end}` : s.exact_start;
  }
  if (s.period && PERIOD_LABEL[s.period]) return PERIOD_LABEL[s.period];
  return null;
}

export interface DayCardProps {
  day: TripDay;
  dayIndex: number;
  city?: string;
  cityCover?: string;
  accommodation?: AccommodationInfo | null;
  mustInclude?: TripMustInclude[] | null;
  activePlaceId?: number | null;
  onPlaceClick: (placeId: number) => void;
  onAccommodationLocationClick: () => void;
  expandedCommutes: Set<string>;
  onToggleCommute: (key: string) => void;
  narrativeExpanded: boolean;
  onToggleNarrative: () => void;
}

/**
 * 每日行程详情卡片组件
 * 包含封面画报大图、主编手账（支持2行截断与展开）、住宿节点、景点时间轴与跨点通勤路线
 */
export function DayCard({
  day,
  dayIndex,
  city,
  cityCover,
  accommodation,
  mustInclude,
  activePlaceId,
  onPlaceClick,
  onAccommodationLocationClick,
  expandedCommutes,
  onToggleCommute,
  narrativeExpanded,
  onToggleNarrative,
}: DayCardProps) {
  return (
    <div className="space-y-6">
      {/* 封面画报大图 */}
      {cityCover && (
        <div className="relative z-10 mb-5 h-48 sm:h-60 w-full overflow-hidden rounded-2xl shadow-soft group">
          <img
            src={cityCover}
            onError={(event) => { event.currentTarget.onerror = null; event.currentTarget.src = "/city-placeholder.svg"; }}
            alt={`${city || ""} 第 ${day.day} 天`}
            loading={dayIndex === 0 ? "eager" : "lazy"}
            className="h-full w-full object-cover transition-transform duration-700 ease-out group-hover:scale-105"
          />
          <div className="absolute inset-0 bg-gradient-to-t from-gray-950/40 via-transparent to-transparent pointer-events-none" />
          <span className="absolute bottom-3 left-3 text-[11px] font-medium text-white/95 bg-black/40 backdrop-blur-md px-2.5 py-1 rounded-full flex items-center gap-1">
            <MapPin size={10} className="text-emerald-300" />
            <span>{city ? `${city} · ` : ""}第 {day.day} 天 · {cityCover === "/city-placeholder.svg" ? "城市图片待补充" : "城市参考配图"}</span>
          </span>
        </div>
      )}

      {/* 主编手账便签（支持两行截断预览与展开） */}
      {day.narrative && (
        <div className="relative z-10 rounded-2xl border border-sand-200 bg-sand-50/85 p-4 sm:p-5">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs font-bold tracking-wider text-primary-800 uppercase">
              <span className="flex h-5 w-5 items-center justify-center rounded-md bg-primary-600 text-white shadow-2xs">
                <Feather size={10} aria-hidden="true" />
              </span>
              <span>主编手账 · 路线要领</span>
            </div>
            <button
              type="button"
              onClick={onToggleNarrative}
              className="text-xs font-semibold text-primary-700 hover:text-primary-900 flex items-center gap-1 transition-colors px-2 py-0.5 rounded-lg hover:bg-primary-50 active:scale-98"
            >
              <span>{narrativeExpanded ? "收起" : "展开全文"}</span>
              <ChevronDown
                size={9}
                className={`transition-transform duration-200 ${
                  narrativeExpanded ? "rotate-180" : ""
                }`}
                aria-hidden="true"
              />
            </button>
          </div>
          <p
            className={`text-sm sm:text-base leading-relaxed text-gray-700 transition-all ${
              narrativeExpanded ? "" : "line-clamp-2"
            }`}
          >
            {day.narrative}
          </p>
          {day.commute_summary && (
            <div className="mt-3 flex items-center gap-2 border-t border-sand-200/80 pt-2.5 text-xs text-primary-800 font-medium">
              <Route size={14} className="text-primary-500" aria-hidden="true" />
              <span>全天出行参考：{day.commute_summary}</span>
            </div>
          )}
        </div>
      )}

      {/* 景点时间轴主干 */}
      <div className="relative z-10 space-y-5">
        <AccommodationTimelineNode
          accommodation={accommodation}
          day={day.day}
          onLocationClick={onAccommodationLocationClick}
        />

        {day.places.map((place, placeIndex) => {
          const nextLeg = day.commute_legs?.find(
            (l) => l.from_place_id === place.place_id,
          );
          const isLast = placeIndex === day.places.length - 1;
          const anchor = isAnchorRole(place.role);
          const timeLabel = placeTimeLabel(place);
          const stayDuration = formatStayDuration(place.stay_minutes);
          const scheduledMustInclude = mustInclude?.find(
            (mi) => mi.status === "scheduled" && mi.place_id != null && mi.place_id === place.place_id,
          );

          const fromPlace = place;
          const toPlace = nextLeg ? day.places.find((p) => p.place_id === nextLeg.to_place_id) : undefined;
          const legKey = nextLeg ? `day-${day.day}-leg-${place.place_id}-${nextLeg.to_place_id}` : "";
          const isLegExpanded = legKey ? expandedCommutes.has(legKey) : false;

          return (
            <div key={place.place_id} className="relative">
              {/* 时间轴竖导轨 */}
              {!isLast && (
                <div
                  className="absolute left-5 sm:left-6 top-14 bottom-0 w-0.5 bg-gradient-to-b from-primary-300 via-sand-300 to-sand-200"
                  aria-hidden="true"
                />
              )}

              {/* 景点节点卡片（纯净统一的手账质感白卡） */}
              <div
                onClick={() => onPlaceClick(place.place_id)}
                className={`group relative flex flex-col rounded-2xl border transition-all duration-200 p-4 sm:p-5 cursor-pointer ${
                  activePlaceId === place.place_id
                    ? "border-primary-500 bg-primary-50/50 shadow-md ring-2 ring-primary-300/60"
                    : anchor
                    ? "border-sand-200/90 bg-white hover:border-primary-300 hover:shadow-md"
                    : "border-gray-100 bg-sand-50/40 hover:border-primary-200 hover:bg-white hover:shadow-sm"
                }`}
              >
                {/* 头部：序号 + 标题 + 右侧属性 */}
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <span
                      className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl font-display text-sm font-bold shadow-xs ${
                        anchor
                          ? "bg-gradient-to-br from-primary-600 to-emerald-600 text-white ring-2 ring-primary-100"
                          : "bg-sand-100 text-gray-700 border border-sand-200"
                      }`}
                    >
                      {String(placeIndex + 1).padStart(2, "0")}
                    </span>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-base" aria-hidden="true">
                          {categoryIcon(place.category)}
                        </span>
                        <h3 className="truncate text-lg font-bold text-gray-900 transition-colors group-hover:text-primary-700">
                          {place.name}
                        </h3>
                        {scheduledMustInclude && (
                          <span className="shrink-0 rounded-md bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 text-[10px] font-bold text-emerald-700">
                            你的必去
                          </span>
                        )}
                        {place.optional && (
                          <span className="shrink-0 rounded-md bg-amber-50 border border-amber-200 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                            可选
                          </span>
                        )}
                        {!anchor && (
                          <span className="shrink-0 rounded-md bg-sand-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">
                            惬意小憩
                          </span>
                        )}
                      </div>

                      {/* 分类与时段标签 */}
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500">
                        <span className="font-medium text-gray-600">
                          {categoryName(place.category)}
                        </span>
                        {timeLabel && (
                          <>
                            <span className="text-gray-300">·</span>
                            <span className="text-primary-700 font-medium">{timeLabel}</span>
                          </>
                        )}
                      </div>
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-2">
                    <span className="flex items-center gap-1 rounded-full bg-sand-100 border border-sand-200 px-2.5 py-0.5 text-xs font-medium text-gray-600">
                      <Clock size={11} className="text-primary-600" />
                      <span>{stayDuration}</span>
                    </span>
                    <span className="ml-1 text-xs font-semibold text-primary-600 opacity-0 group-hover:opacity-100 transition-opacity hidden sm:inline-flex items-center gap-0.5">
                      查看 <ChevronRight size={10} />
                    </span>
                  </div>
                </div>

                {/* 详情与说明描述 */}
                {(cleanBrief(place.brief) || place.activity_note) && (
                  <div className="mt-3 border-t border-gray-100/90 pt-3 text-sm leading-relaxed">
                    {place.activity_note ? (
                      <p className="font-normal text-gray-700">{place.activity_note}</p>
                    ) : (
                      <p className="text-gray-500">{cleanBrief(place.brief)}</p>
                    )}
                  </div>
                )}
              </div>

              {/* 跨点通勤交互微卡片 */}
              {nextLeg && (() => {
                const NextLegIcon = commuteModeIcon(nextLeg.mode);
                return (
                  <div className="my-3 ml-5 sm:ml-6 space-y-2">
                    {/* 胶囊控制条 */}
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => onToggleCommute(legKey)}
                        className="group flex items-center gap-2 rounded-full border border-primary-200/90 bg-primary-50/90 px-3 py-1 text-xs font-medium text-primary-800 shadow-2xs transition-all hover:bg-primary-100/90 hover:shadow-xs active:scale-98"
                        title="点击查看此段详细路线"
                      >
                        <NextLegIcon size={11} className="text-primary-600" />
                        <span>
                          {commuteModeName(nextLeg.mode)} {formatMinutes(nextLeg.duration_minutes)}
                        </span>
                        <span className="text-primary-300">·</span>
                        <span className="text-primary-700 font-bold">
                          {formatDistance(nextLeg.distance_meters)}
                        </span>
                        {toPlace && (
                          <span className="ml-1 flex items-center gap-1 text-primary-700 font-semibold group-hover:text-primary-950 transition-colors">
                            <ArrowRight size={9} className="text-primary-400" />
                            <span className="truncate max-w-[120px]">{toPlace.name}</span>
                            <ChevronDown size={9} className={`transition-transform duration-200 ${isLegExpanded ? "rotate-180" : ""}`} />
                          </span>
                        )}
                      </button>
                    </div>

                    {/* 展开态：详细路线指引卡 */}
                    {isLegExpanded && (
                      <div className="animate-fade-in relative overflow-hidden rounded-2xl border border-primary-200/90 bg-gradient-to-br from-primary-50/95 via-emerald-50/40 to-white p-4 shadow-sm space-y-2.5">
                        <div className="flex items-center justify-between gap-2 border-b border-primary-100/80 pb-2">
                          <div className="flex items-center gap-1.5 text-xs font-bold text-gray-800">
                            <span className="flex h-5 w-5 items-center justify-center rounded-full bg-primary-100 text-primary-800 text-[10px]">
                              起
                            </span>
                            <span className="truncate max-w-[100px] sm:max-w-[160px]">{fromPlace.name}</span>
                            <ArrowRight size={10} className="text-primary-400 mx-1" />
                            <span className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-100 text-emerald-800 text-[10px]">
                              终
                            </span>
                            <span className="truncate max-w-[100px] sm:max-w-[160px]">{toPlace?.name || "下一站"}</span>
                          </div>
                          <span className="shrink-0 text-xs font-bold text-primary-700">
                            {commuteModeName(nextLeg.mode)}约 {formatMinutes(nextLeg.duration_minutes)}
                          </span>
                        </div>

                        {nextLeg.transit_summary && (
                          <div className="flex items-start gap-1.5 text-xs text-gray-600 leading-relaxed">
                            <CornerUpRight size={11} className="text-primary-500 mt-0.5 shrink-0" />
                            <span>{nextLeg.transit_summary}</span>
                          </div>
                        )}

                        {fromPlace.longitude != null && fromPlace.latitude != null && toPlace?.longitude != null && toPlace?.latitude != null && (
                          <div className="pt-1 flex justify-end">
                            <a
                              href={`https://uri.amap.com/navigation?from=${fromPlace.longitude},${fromPlace.latitude},${encodeURIComponent(fromPlace.name)}&to=${toPlace.longitude},${toPlace.latitude},${encodeURIComponent(toPlace.name)}&mode=${nextLeg.mode === "walking" ? "walk" : (nextLeg.mode === "transit" ? "bus" : "car")}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-1 text-[11px] font-semibold text-primary-700 hover:text-primary-900 transition-colors"
                            >
                              <Navigation size={10} />
                              <span>在高德地图中导航此段路线 ↗</span>
                            </a>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>
          );
        })}
      </div>
    </div>
  );
}


