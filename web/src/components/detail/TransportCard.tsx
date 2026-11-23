import { useState } from "react";
import type { TripTransport, TransportMode, TransportOption } from "@/types/trip";
import { ChevronUp, ChevronDown, Plane, TrainFront, TriangleAlert } from "lucide-react";

interface TransportCardProps {
  data: TripTransport | null | undefined;
  selectedMode?: "train" | "flight" | null;
  onSelectMode?: (mode: "train" | "flight") => void;
}

function formatDuration(minutes: number): string {
  if (minutes <= 0) return "0分";
  const hrs = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (hrs === 0) return `${mins}分`;
  if (mins === 0) return `${hrs}小时`;
  return `${hrs}小时${mins}分`;
}

function formatCheckedTime(utcIso: string | null): string | null {
  if (!utcIso) return null;
  try {
    const d = new Date(utcIso);
    if (Number.isNaN(d.getTime())) return null;
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const hours = String(d.getHours()).padStart(2, "0");
    const mins = String(d.getMinutes()).padStart(2, "0");
    return `${month}-${day} ${hours}:${mins}`;
  } catch {
    return null;
  }
}

function ModeOptionsList({ mode }: { mode: TransportMode }) {
  if (!mode.options || mode.options.length === 0) return null;

  return (
    <div className="mt-3 space-y-2 border-t border-dashed border-gray-200 pt-3">
      {mode.options.map((opt: TransportOption, idx: number) => {
        const titleText =
          opt.type === "flight" && opt.airline
            ? `${opt.airline} ${opt.no}`
            : opt.no;
        return (
          <div
            key={`${opt.no}-${idx}`}
            className="flex flex-wrap items-center justify-between text-xs text-gray-700 sm:flex-nowrap"
          >
            <div className="flex items-center gap-2 font-mono font-medium text-gray-900 min-w-[90px]">
              <span
                className="inline-flex shrink-0 items-center font-normal text-primary-600"
                aria-hidden="true"
              >
                {opt.type === "flight" ? (
                  <Plane size={14} style={{ width: 14, height: 14 }} />
                ) : (
                  <TrainFront size={14} style={{ width: 14, height: 14 }} />
                )}
              </span>
              <span>{titleText}</span>
            </div>
            <div className="flex items-center gap-1.5 tabular-nums text-gray-600">
              <span>{opt.departure_time}</span>
              <span className="text-gray-400">{opt.departure_station}</span>
              <span className="text-gray-300">→</span>
              <span>{opt.arrival_time}</span>
              <span className="text-gray-400">{opt.arrival_station}</span>
            </div>
            <div className="flex items-center gap-3 tabular-nums ml-auto sm:ml-0">
              <span className="text-gray-500">{formatDuration(opt.duration_minutes)}</span>
              {opt.price ? (
                <span className="font-bold text-gray-900">{opt.price}</span>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

interface ModeRowProps {
  mode: TransportMode;
  isSelected?: boolean;
  onSelect?: () => void;
}

function ModeRow({ mode, isSelected, onSelect }: ModeRowProps) {
  const [expanded, setExpanded] = useState(false);
  const isFlight = mode.mode === "flight";
  const modeTitle = isFlight ? "飞机" : "高铁";
  const hasOptions = mode.options && mode.options.length > 0;
  const showStaticBadge =
    mode.price_source === "static_reference" &&
    !mode.price_range.includes("参考");

  return (
    <div
      onClick={onSelect}
      className={`rounded-xl p-3.5 border transition-all ${
        onSelect ? "cursor-pointer" : ""
      } ${
        isSelected
          ? "border-primary-500 bg-primary-50/40 ring-1 ring-primary-500/20 shadow-xs"
          : onSelect
            ? "border-gray-100 bg-gray-50/80 hover:border-primary-200 hover:bg-gray-50"
            : "border-gray-100 bg-gray-50/80"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
        {/* 等权摘要行：图标 + 模式 + 耗时 + 价格 + 班次 */}
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="font-bold text-gray-900 flex items-center gap-1">
            <span
              className="inline-flex items-center text-primary-600"
              aria-hidden="true"
            >
              {isFlight ? (
                <Plane size={15} style={{ width: 15, height: 15 }} />
              ) : (
                <TrainFront size={15} style={{ width: 15, height: 15 }} />
              )}
            </span>
            <span>{modeTitle}</span>
          </span>
          {isSelected && (
            <span className="rounded-full bg-primary-100 px-2 py-0.5 text-[10px] font-bold text-primary-700">
              已选
            </span>
          )}
          <span className="text-gray-300">·</span>
          <span className="text-gray-600 tabular-nums">
            最快 {formatDuration(mode.min_duration_minutes)}
          </span>
          <span className="text-gray-300">·</span>
          <span className="font-bold text-primary-700 tabular-nums">
            {mode.price_range}
          </span>
          {showStaticBadge && (
            <span className="rounded bg-gray-200/60 px-1.5 py-0.5 text-[10px] text-gray-500">
              参考
            </span>
          )}
          {mode.daily_count > 0 && (
            <>
              <span className="text-gray-300">·</span>
              <span className="text-gray-500 tabular-nums">
                每日约 {mode.daily_count} 班
              </span>
            </>
          )}
        </div>

        {/* 展开 Top5 参考入口 */}
        {hasOptions && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            className="inline-flex min-h-[44px] min-w-[44px] items-center gap-1 text-xs font-medium text-primary-600 hover:text-primary-800"
          >
            <span>{expanded ? "收起参考" : isFlight ? "航班参考 (Top5)" : "车次参考 (Top5)"}</span>
            {expanded ? (
              <ChevronUp size={10} aria-hidden="true" />
            ) : (
              <ChevronDown size={10} aria-hidden="true" />
            )}
          </button>
        )}
      </div>

      {/* 售罄提醒 */}
      {mode.availability_status === "sold_out_at_query" && (
        <div className="mt-2.5 flex items-center gap-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 border border-amber-200">
          <TriangleAlert size={14} className="text-amber-500 shrink-0" aria-hidden="true" />
          <span>查询时段车票已售罄，请以购票平台实时余票为准</span>
        </div>
      )}

      {/* Top5 班次参考区 */}
      {expanded && <ModeOptionsList mode={mode} />}
    </div>
  );
}

export function TransportCard({
  data,
  selectedMode,
  onSelectMode,
}: TransportCardProps) {
  if (!data || !data.modes || data.modes.length === 0) {
    return null;
  }

  // 找首个有 options 的 station 名称作为示意
  const firstOption = data.modes.flatMap((m) => m.options ?? [])[0];
  const depStation = firstOption?.departure_station ?? data.from_city;
  const arrStation = firstOption?.arrival_station ?? data.to_city;

  // 确定顶图标
  const hasTrain = data.modes.some((m) => m.mode === "train");
  const hasFlight = data.modes.some((m) => m.mode === "flight");
  const showTrain = hasTrain || !hasFlight;

  // 收集最晚的余票查询时间（ISO 最大值）
  const checkedIsoList = data.modes
    .map((m) => m.availability_checked_at)
    .filter((t): t is string => Boolean(t))
    .sort();
  const latestCheckedIso = checkedIsoList.length > 0 ? checkedIsoList[checkedIsoList.length - 1] : null;
  const latestChecked = formatCheckedTime(latestCheckedIso);

  return (
    <div className="space-y-3">
      <div className="mb-1 flex items-center justify-between px-1">
        <h3 className="text-xs font-bold uppercase tracking-wider text-gray-500">
          去程大交通推荐
        </h3>
        {data.query_date && (
          <span className="text-[11px] text-gray-400 tabular-nums">
            出行日期: {data.query_date}
          </span>
        )}
      </div>

      <div className="rounded-2xl border border-primary-100 bg-white p-5 shadow-soft space-y-4">
        {/* 城市大标题对排与连线 */}
        <div className="flex items-center justify-between">
          <div>
            <div className="font-display text-2xl font-bold text-gray-900">
              {data.from_city}
            </div>
            <div className="text-xs text-gray-500">{depStation}</div>
          </div>

          <div className="flex flex-1 flex-col items-center px-4">
            <div className="flex w-full items-center gap-2">
              <div className="h-[1px] flex-1 bg-gray-200" />
              <span
                className="inline-flex items-center gap-1 text-base text-primary-600"
                aria-hidden="true"
              >
                {showTrain && (
                  <TrainFront size={18} style={{ width: 18, height: 18 }} />
                )}
                {hasTrain && hasFlight && (
                  <span className="text-xs text-gray-300">/</span>
                )}
                {hasFlight && (
                  <Plane size={18} style={{ width: 18, height: 18 }} />
                )}
              </span>
              <div className="h-[1px] flex-1 bg-gray-200" />
            </div>
          </div>

          <div className="text-right">
            <div className="font-display text-2xl font-bold text-gray-900">
              {data.to_city}
            </div>
            <div className="text-xs text-gray-500">{arrStation}</div>
          </div>
        </div>

        {/* 票根撕拉打孔虚线 */}
        <div className="relative">
          <div className="border-t border-dashed border-gray-200" />
        </div>

        {/* 1~2 行等权的 mode 摘要 */}
        <div className="space-y-2.5">
          {data.modes.map((m: TransportMode, idx: number) => (
            <ModeRow
              key={`${m.mode}-${idx}`}
              mode={m}
              isSelected={selectedMode === m.mode}
              onSelect={onSelectMode ? () => onSelectMode(m.mode) : undefined}
            />
          ))}
        </div>

        {/* 免责/提示尾注 */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-gray-100 pt-2.5 text-[11px] text-gray-400">
          {data.source === "static_fallback" || data.query_date === null ? (
            <span>静态估算，班次与价格以购票平台为准</span>
          ) : (
            <span>实时推荐方案</span>
          )}
          {latestChecked && <span>余票查询于 {latestChecked}</span>}
        </div>
      </div>
    </div>
  );
}
