import { useEffect, useState, type CSSProperties } from "react";
import type { TripDay, TripPlace } from "@/types/trip";
import { categoryIcon, isAnchorRole } from "@/constants/places";
import { commuteModeName, commuteModeIcon, formatMinutes, formatDistance, cleanBrief } from "@/utils/format";
import { periodLabel, governedExactTime, exactTimeSourceLabel } from "@/utils/schedule";
import { ChevronRight, Clock, Lightbulb, ChevronDown } from "lucide-react";

interface TimelineProps {
  day: TripDay;
  activePlaceId?: number | null;
  onPlaceClick?: (place: TripPlace) => void;
}

/** 时段徽标配色：一天内的视觉节奏由浅入深 */
const PERIOD_BADGE_CLASS: Record<string, string> = {
  上午: "bg-amber-50 text-amber-600",
  下午: "bg-sky-50 text-sky-600",
  傍晚: "bg-orange-50 text-orange-600",
  晚上: "bg-indigo-50 text-indigo-600",
};

export function Timeline({ day, activePlaceId, onPlaceClick }: TimelineProps) {
  const [narrativeOpen, setNarrativeOpen] = useState(true);

  useEffect(() => {
    setNarrativeOpen(true);
  }, [day.day]);

  return (
    <div className="px-5 py-5">
      <div className="stagger relative space-y-1">
        {day.places.map((place, i) => {
          const leg = day.commute_legs.find((l) => l.from_place_id === place.place_id);
          const anchor = isAnchorRole(place.role);
          const isActive = activePlaceId === place.place_id;
          // 时段与精确时间只来自后端 schedule；1.4 结果无此字段则不显示任何时间
          const period = periodLabel(place.schedule?.period);
          const exact = governedExactTime(place.schedule);
          const note = cleanBrief(place.activity_note);
          const brief = cleanBrief(place.brief);

          return (
            <div key={place.place_id} className="relative group" style={{ "--i": i } as CSSProperties}>
              {/* 连线：放在最外层，贯穿整个节点和通勤区域 */}
              {i < day.places.length - 1 && (
                <div
                  className="absolute top-8 bottom-0 w-px bg-gray-200"
                  style={{ left: "76px" }}
                  aria-hidden="true"
                />
              )}
              <div className="relative z-10 flex gap-4">
                {/* 时段列（后端 period；无 schedule 的旧结果留白） */}
                <div className="w-12 shrink-0 pt-2 text-right">
                  {period && (
                    <span
                      data-testid="period-badge"
                      className={`inline-block rounded-md px-1.5 py-0.5 text-[11px] font-medium ${
                        PERIOD_BADGE_CLASS[period] ?? "bg-gray-50 text-gray-600"
                      }`}
                    >
                      {period}
                    </span>
                  )}
                </div>

                {/* 节点 */}
                <div className="relative shrink-0 pt-2">
                  <div
                    className={`relative z-10 flex h-6 w-6 items-center justify-center rounded-full text-xs font-bold text-white shadow-sm ${
                      anchor ? "bg-primary-600" : "bg-accent-400"
                    }`}
                  >
                    {i + 1}
                  </div>
                </div>

                {/* 停靠点：清单行；弱底 + 常显 chevron，避免裸文被当成不可点 */}
                <button
                  type="button"
                  onClick={() => onPlaceClick?.(place)}
                  aria-label={`查看${place.name}详情`}
                  aria-current={isActive ? "true" : undefined}
                  className={`group/place min-w-0 flex-1 cursor-pointer rounded-lg border px-2.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-300 focus-visible:ring-offset-1 ${
                    exact || note || brief ? "py-2.5" : "py-2"
                  } ${
                    isActive
                      ? "border-primary-200 bg-primary-50"
                      : "border-transparent bg-gray-50/70 hover:border-gray-100 hover:bg-gray-100/80 active:bg-gray-100"
                  }`}
                >
                  <div className="flex w-full items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span className="shrink-0 text-sm" aria-hidden="true">
                        {categoryIcon(place.category)}
                      </span>
                      <h3
                        className={`min-w-0 truncate text-sm font-semibold transition-colors ${
                          isActive
                            ? "text-primary-700"
                            : "text-gray-800 group-hover/place:text-primary-700"
                        }`}
                      >
                        {place.name}
                      </h3>
                      {place.optional && (
                        <span className="shrink-0 rounded bg-white/80 px-1.5 py-0.5 text-[10px] text-gray-500">
                          可选
                        </span>
                      )}
                    </div>
                    <ChevronRight
                      size={10}
                      className={`shrink-0 transition-transform ${
                        isActive
                          ? "translate-x-0.5 text-primary-500"
                          : "text-gray-300 group-hover/place:translate-x-0.5 group-hover/place:text-primary-400"
                      }`}
                      aria-hidden="true"
                    />
                  </div>

                  {/* 精确时间：仅受治理来源（reservation/event/transport/verified_venue_rule/planned） */}
                  {exact && (
                    <div className="mt-1 flex items-center gap-1.5 pl-5 text-[11px] font-medium text-gray-600 tabular-nums">
                      <Clock size={12} aria-hidden="true" />
                      {exact.text}
                      <span className="rounded bg-primary-50 px-1 py-0.5 text-[10px] font-normal text-primary-600">
                        {exactTimeSourceLabel(exact.source)}
                      </span>
                    </div>
                  )}

                  {/* activity_note：本条路线中该地点的体验说明（1.5）；旧结果回退 brief */}
                  {note ? (
                    <p className="mt-0.5 whitespace-pre-line pl-5 text-[12px] leading-relaxed text-gray-500">
                      {note}
                    </p>
                  ) : (
                    brief && (
                      <p className="mt-0.5 line-clamp-2 pl-5 text-[11px] leading-relaxed text-gray-500">
                        {brief}
                      </p>
                    )
                  )}
                </button>
              </div>

              {/* 通勤段：与上下景点拉开间距，作为两站之间的桥梁，而非上站附属 */}
              {leg && (() => {
                const LegIcon = commuteModeIcon(leg.mode);
                return (
                <div className="relative z-10 mt-3 mb-3 flex gap-4 py-1">
                  <div className="w-12 shrink-0" />
                  <div className="w-6 shrink-0" />
                  <div className="flex flex-col gap-1">
                    <div
                      className="flex shrink-0 whitespace-nowrap items-center gap-2 rounded-md border border-gray-100 px-2.5 py-1.5 text-[11px] font-medium text-gray-500 tabular-nums"
                      style={{ backgroundColor: "var(--color-bg-secondary)" }}
                    >
                      <LegIcon
                        size={10}
                        className="text-gray-400"
                        aria-hidden="true"
                      />
                      {commuteModeName(leg.mode)} {formatMinutes(leg.duration_minutes)}
                      <span className="text-gray-300">|</span>
                      {formatDistance(leg.distance_meters)}
                    </div>
                    {leg.mode === "transit" &&
                      (leg.transit_steps?.length ? (
                        <div className="max-w-[280px] space-y-1">
                          {leg.transit_steps.map((step, si) => (
                            <p key={si} className="text-[10px] leading-relaxed text-gray-500">
                              {step.kind === "bus" || step.kind === "rail" ? (
                                <>
                                  <span className="font-medium text-gray-600">
                                    {step.line_name ?? commuteModeName(step.kind)}
                                  </span>
                                  {step.from_stop && step.to_stop && (
                                    <span>
                                      {" "}
                                      · {step.from_stop} → {step.to_stop}
                                    </span>
                                  )}
                                  {step.stop_count != null && <span>（{step.stop_count}站）</span>}
                                  {step.duration_minutes != null && (
                                    <span> {formatMinutes(step.duration_minutes)}</span>
                                  )}
                                </>
                              ) : (
                                <>
                                  {step.kind === "walking" ? "步行" : "换乘"}
                                  {step.duration_minutes != null &&
                                    ` ${formatMinutes(step.duration_minutes)}`}
                                  {step.distance_meters != null &&
                                    ` ${formatDistance(step.distance_meters)}`}
                                </>
                              )}
                            </p>
                          ))}
                        </div>
                      ) : leg.transit_summary ? (
                        <p className="max-w-[260px] text-[10px] leading-relaxed text-gray-400">
                          {leg.transit_summary}
                        </p>
                      ) : null)}
                  </div>
                </div>
                );
              })()}
            </div>
          );
        })}
      </div>

      {/* 当天概述：时间轴之后，默认展开，不改写内容 */}
      {day.narrative && (
        <div className="mt-6 rounded-2xl border border-amber-100/50 bg-amber-50/60 p-4">
          <button
            type="button"
            onClick={() => setNarrativeOpen((v) => !v)}
            className="flex min-h-[44px] w-full items-center justify-between gap-2 rounded-lg py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-300 md:pointer-events-none"
            aria-expanded={narrativeOpen}
          >
            <span className="flex items-center gap-2 text-[14px] font-bold text-amber-800">
              <Lightbulb size={18} className="text-amber-500" aria-hidden="true" />
              当天怎么玩
            </span>
            <ChevronDown
              size={12}
              className={`text-amber-500 transition-transform md:hidden ${
                narrativeOpen ? "rotate-180" : ""
              }`}
              aria-hidden="true"
            />
          </button>
          {narrativeOpen && (
            <div className="mt-3 text-[13px] leading-relaxed text-amber-900/80">
              {day.narrative.split("\n").map((line, idx) => (
                <p key={idx} className={idx > 0 ? "mt-1.5" : ""}>
                  {line}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
