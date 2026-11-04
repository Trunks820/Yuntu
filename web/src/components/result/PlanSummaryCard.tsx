import type { CSSProperties } from "react";
import type { TripPlan } from "@/types/trip";
import { cleanSummary, cleanTags } from "@/utils/format";
import { ThumbsUp, Gauge, Route, ChevronRight } from "lucide-react";

interface PlanSummaryCardProps {
  plan: TripPlan;
  image: string;
  recommended?: boolean;
  index?: number;
  onClick: () => void;
}

const PACE_LABEL: Record<string, string> = {
  RELAXED: "轻松",
  MODERATE: "适中",
  INTENSIVE: "紧凑",
};

// 每张拍立得的轻微倾斜，按索引轮换；hover 转正
const ROTATIONS = ["-rotate-2", "rotate-1", "-rotate-1", "rotate-2"];

export function PlanSummaryCard({ plan, image, recommended, index = 0, onClick }: PlanSummaryCardProps) {
  const paceText = PACE_LABEL[plan.pace.level] ?? "适中";
  const hours = Math.floor(plan.pace.total_commute_minutes / 60);
  const mins = plan.pace.total_commute_minutes % 60;
  const commuteText = hours > 0 ? `${hours}小时${mins}分钟` : `${mins}分钟`;
  const rotation = ROTATIONS[index % ROTATIONS.length];

  return (
    <button
      type="button"
      onClick={onClick}
      style={{ "--i": index } as CSSProperties}
      className={`group flex h-full flex-col rounded-sm bg-white p-3 pb-0 text-left shadow-[0_12px_30px_rgba(15,80,75,0.14)] transition-transform duration-300 hover:-translate-y-1 hover:rotate-0 ${rotation}`}
    >
      {/* 拍立得相片 */}
      <div className="relative h-44 overflow-hidden rounded-sm sm:h-48">
        <img src={image} alt={plan.title} className="h-full w-full object-cover" />
        {recommended && (
          <div className="absolute left-3 top-3 flex items-center rounded-full bg-accent-500 px-3 py-1 text-xs font-bold text-white shadow-md">
            <ThumbsUp size={14} className="mr-1.5" aria-hidden="true" /> 推荐
          </div>
        )}
      </div>

      {/* 手写体标题（相片下方留白区） */}
      <h3 className="signature-font px-1 pt-3 text-center text-2xl leading-tight text-gray-800">
        {plan.title}
      </h3>

      <div className="flex flex-1 flex-col px-2 pb-4 pt-1">
        <p className="mb-3 line-clamp-2 text-center text-xs leading-relaxed text-gray-500">
          {cleanSummary(plan.summary)}
        </p>

        <div className="mb-3 flex flex-wrap justify-center gap-1.5">
          {cleanTags(plan.tags).slice(0, 4).map((tag) => (
            <span key={tag} className="rounded-full bg-primary-50 px-2 py-0.5 text-[10px] text-primary-600">
              {tag}
            </span>
          ))}
        </div>

        {/* 节奏 + 通勤（真实数据） */}
        <div className="mt-auto flex items-center justify-between border-t border-dashed border-gray-200 pt-3">
          <div className="flex flex-col">
            <span className="mb-0.5 flex items-center text-[11px] text-gray-400">
              <Gauge size={14} className="mr-1 text-primary-600" aria-hidden="true" /> 行程节奏
            </span>
            <span className="text-base font-bold text-primary-700">{paceText}</span>
          </div>
          <div className="flex flex-col items-end">
            <span className="mb-0.5 flex items-center text-[11px] text-gray-400">
              <Route size={14} className="mr-1 text-primary-600" aria-hidden="true" /> 总通勤
            </span>
            <span className="text-sm font-semibold text-gray-700">{commuteText}</span>
          </div>
        </div>

        <div className="mt-3 flex items-center justify-center gap-1 text-sm font-semibold text-primary-600 transition-transform group-hover:translate-x-0.5">
          查看详情 <ChevronRight size={12} aria-hidden="true" />
        </div>
      </div>
    </button>
  );
}


