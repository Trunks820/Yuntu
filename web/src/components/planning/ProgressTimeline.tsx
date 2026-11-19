import { STAGE_MAP, TOTAL_STAGES } from "@/constants/stages";
import type { StageCode } from "@/types/trip";
import { CircleAlert, Check } from "lucide-react";

interface ProgressTimelineProps {
  currentCode: StageCode | null;
  failed?: boolean;
}

const ORDERED_CODES: StageCode[] = ["ANALYZING", "PLANNING", "COMPOSING", "FINALIZING"];

const STAGE_HINTS: Record<StageCode, string> = {
  ANALYZING: "偏好、节奏与预算深度解析",
  PLANNING: "真实地点筛选与通勤路线优化",
  COMPOSING: "每日日程与沉浸式体验编排",
  FINALIZING: "逻辑校验、合理性闭环与整理",
};

export function ProgressTimeline({ currentCode, failed }: ProgressTimelineProps) {
  const currentStep = currentCode ? STAGE_MAP[currentCode].step : (failed ? 0 : 1);

  return (
    <div className="relative select-none">
      {/* 竖向高精导轨 */}
      <div className="relative pl-10">
        {/* 底层纤细背景轨道 */}
        <span
          className="absolute left-[13px] top-3.5 bottom-3.5 w-[1.5px] bg-gradient-to-b from-primary-900/15 via-primary-900/10 to-transparent"
          aria-hidden="true"
        />

        {/* 动态流光能量管 */}
        <span
          className="absolute left-[13px] top-3.5 w-[1.5px] bg-gradient-to-b from-emerald-500 via-teal-500 to-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.5)] transition-all duration-700 ease-out"
          style={{
            height: failed
              ? `${Math.max(0, (currentStep - 1) * 33)}%`
              : `${Math.min(100, Math.max(0, (currentStep - 1) * 33))}%`,
          }}
          aria-hidden="true"
        />

        <ol className="space-y-6">
          {ORDERED_CODES.map((code) => {
            const info = STAGE_MAP[code];
            const isDone = info.step < currentStep;
            const isActive = info.step === currentStep && !failed;
            const isFailed = failed && info.step === currentStep && currentStep > 0;
            const labelText = failed ? info.label.replace(/^正在/, "") : info.label;

            return (
              <li key={code} className="relative group">
                {/* 节点视觉：从物流大勾升级为精密呼吸光环 */}
                <div className="absolute -left-10 top-0 flex items-center justify-center">
                  {isFailed ? (
                    <span className="flex h-7 w-7 items-center justify-center rounded-full bg-red-500 text-white shadow-[0_0_12px_rgba(239,68,68,0.45)] ring-4 ring-red-100 animate-pulse">
                      <CircleAlert size={11} aria-hidden="true" />
                    </span>
                  ) : isDone ? (
                    <span className="flex h-7 w-7 items-center justify-center rounded-full bg-white/90 border border-emerald-500/40 text-emerald-600 shadow-[0_2px_8px_rgba(16,185,129,0.15)] backdrop-blur-xs transition-transform duration-300 group-hover:scale-110">
                      <Check size={10} className="text-emerald-600" aria-hidden="true" />
                    </span>
                  ) : isActive ? (
                    <div className="relative flex h-7 w-7 items-center justify-center">
                      {/* 外层扩散脉冲光环 */}
                      <span className="absolute inset-0 rounded-full bg-accent-500/20 animate-ping" />
                      <span className="absolute -inset-1 rounded-full bg-accent-400/20 blur-xs" />
                      {/* 核心发光晶体 */}
                      <span className="relative flex h-6 w-6 items-center justify-center rounded-full bg-gradient-to-br from-accent-500 to-accent-300 text-white shadow-[0_0_14px_rgba(163,91,72,0.5)] border border-white/80">
                        <span className="h-2 w-2 rounded-full bg-white animate-pulse" />
                      </span>
                    </div>
                  ) : (
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-white/50 border border-gray-300/60 text-gray-400 text-[10px] font-mono backdrop-blur-2xs transition-colors group-hover:bg-white/80">
                      0{info.step}
                    </span>
                  )}
                </div>

                {/* 阶段文本与呼吸卡片 */}
                <div
                  className={`transition-all duration-300 rounded-xl px-3 py-1.5 -ml-1 ${
                    isActive
                      ? "bg-white/70 backdrop-blur-md shadow-[0_4px_20px_-4px_rgba(0,0,0,0.06),0_0_0_1px_rgba(255,255,255,0.8)] border border-amber-200/50 -translate-y-0.5"
                      : isDone
                        ? "bg-transparent opacity-85 hover:bg-white/30"
                        : "bg-transparent opacity-50"
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <h3
                      className={`text-[15px] font-bold tracking-tight transition-colors ${
                        isFailed
                          ? "text-red-600"
                          : isActive
                            ? "text-gray-900"
                            : isDone
                              ? "text-gray-800"
                              : "text-gray-500"
                      }`}
                    >
                      {labelText}
                    </h3>

                    {isActive && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-accent-50/90 border border-accent-200 px-2 py-0.5 text-[10px] font-bold text-accent-700 shadow-2xs">
                        <span className="h-1.5 w-1.5 rounded-full bg-accent-500 animate-pulse" />
                        深度推演中
                      </span>
                    )}

                    {isDone && (
                      <span className="font-mono text-[10px] text-emerald-600 font-semibold opacity-75">
                        DONE
                      </span>
                    )}
                  </div>

                  <p
                    className={`text-xs mt-0.5 leading-relaxed font-medium transition-colors ${
                      isActive
                        ? "text-gray-600"
                        : isDone
                          ? "text-gray-500"
                          : "text-gray-400"
                    }`}
                  >
                    {STAGE_HINTS[code]}
                  </p>
                </div>
              </li>
            );
          })}
        </ol>
      </div>

      {/* 屏幕阅读器播报当前进度 */}
      <span className="sr-only" role="status">
        {failed
          ? currentCode
            ? `本次规划未完成，停止于第 ${currentStep} 步 ${STAGE_MAP[currentCode].label}`
            : "本次规划未完成"
          : currentCode
            ? `${STAGE_MAP[currentCode].label}，第 ${currentStep} 步，共 ${TOTAL_STAGES} 步`
            : "正在开始"}
      </span>
    </div>
  );
}
