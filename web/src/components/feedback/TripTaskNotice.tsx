import { useNavigate } from "react-router-dom";
import { useTripTaskStore, StoredTripTask } from "@/stores/tripTaskStore";
import { MapPinned, CircleAlert, X, ArrowRight, Search } from "lucide-react";
import { getOutcomeDisplay, getQuotaNotice } from "@/constants/delivery";

export function TripTaskNotice() {
  const navigate = useNavigate();
  const tasks = useTripTaskStore((s) => s.tasks);
  const acknowledgeTask = useTripTaskStore((s) => s.acknowledgeTask);

  // 筛选待展示通知的持久任务 (notificationState === "unread")
  const unreadTasks = Object.values(tasks).filter(
    (t): t is StoredTripTask =>
      t.notificationState === "unread" &&
      (t.status === "ready" || t.status === "failed" || t.status === "timeout" || t.status === "no_route"),
  );

  if (unreadTasks.length === 0) return null;

  // 优先展示最新完成/失败的攻略任务
  const activeTask = unreadTasks.sort(
    (a, b) => (b.finishedAt || b.startedAt) - (a.finishedAt || a.startedAt),
  )[0];

  const isReady = activeTask.status === "ready";
  const isNoRoute = activeTask.status === "no_route";
  const display = activeTask.delivery
    ? getOutcomeDisplay(activeTask.delivery.outcome)
    : {
        label: isReady ? "旅行攻略已生成" : "旅行攻略生成失败",
        color: isReady ? "emerald" : "red",
        icon: isReady ? "check" : "alert",
      };

  const quotaNotice = activeTask.delivery
    ? getQuotaNotice(activeTask.delivery.quota_state)
    : null;

  const handleClose = () => {
    acknowledgeTask(activeTask.jobId);
  };

  const handleAction = () => {
    acknowledgeTask(activeTask.jobId);
    if ((isReady || isNoRoute) && activeTask.resultRecordId) {
      navigate(`/result/${activeTask.resultRecordId}?job_id=${activeTask.jobId}`);
    } else {
      navigate("/");
    }
  };

  const colorClasses = {
    emerald: { bg: "bg-emerald-50", text: "text-emerald-600", ring: "ring-emerald-200/50", gradient: "from-emerald-500 to-teal-500", btn: "bg-emerald-600 hover:bg-emerald-700 focus-visible:ring-emerald-400" },
    amber: { bg: "bg-amber-50", text: "text-amber-600", ring: "ring-amber-200/50", gradient: "from-amber-500 to-orange-500", btn: "bg-amber-600 hover:bg-amber-700 focus-visible:ring-amber-400" },
    red: { bg: "bg-red-50", text: "text-red-600", ring: "ring-red-200/50", gradient: "from-red-500 to-pink-500", btn: "bg-red-600 hover:bg-red-700 focus-visible:ring-red-400" },
    blue: { bg: "bg-blue-50", text: "text-blue-600", ring: "ring-blue-200/50", gradient: "from-blue-500 to-cyan-500", btn: "bg-blue-600 hover:bg-blue-700 focus-visible:ring-blue-400" },
    gray: { bg: "bg-gray-50", text: "text-gray-600", ring: "ring-gray-200/50", gradient: "from-gray-500 to-slate-500", btn: "bg-gray-600 hover:bg-gray-700 focus-visible:ring-gray-400" },
  };

  const c = colorClasses[display.color as keyof typeof colorClasses] || colorClasses.gray;

  return (
    <aside
      aria-live="polite"
      aria-atomic="true"
      className="fixed bottom-4 left-4 right-4 z-[100] sm:bottom-24 sm:left-auto sm:right-6 sm:w-96 animate-slide-up"
    >
      <div
        role={isReady || isNoRoute ? "status" : "alert"}
        className="relative overflow-hidden rounded-2xl border border-gray-100 bg-white/95 p-5 shadow-2xl backdrop-blur-md ring-1 ring-black/5"
      >
        {/* 顶部装饰条 */}
        <div
          className={`absolute top-0 left-0 right-0 h-1 bg-gradient-to-r ${c.gradient}`}
        />

        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3.5">
            <div
              className={`mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl font-bold shadow-xs ${c.bg} ${c.text} ring-1 ${c.ring}`}
            >
              {isNoRoute ? (
                <Search size={18} aria-hidden="true" />
              ) : isReady ? (
                <MapPinned size={18} aria-hidden="true" />
              ) : (
                <CircleAlert size={18} aria-hidden="true" />
              )}
            </div>

            <div className="min-w-0">
              <h3 className="font-display text-base font-bold text-gray-900 tracking-tight">
                {display.label}
              </h3>
              <p className="mt-1 text-xs leading-relaxed font-medium text-gray-600">
                {isReady
                  ? `为你准备好了 ${activeTask.destination || "目的地"} 的行程攻略`
                  : isNoRoute
                  ? `在限定范围内未能规划出 ${activeTask.destination || "目的地"} 的完整路线`
                  : `${activeTask.destination || "目的地"} 行程生成未完成，可点击重新规划`}
              </p>
              {quotaNotice && (
                <p className="mt-1.5 text-[10px] text-gray-500 font-medium">
                  {quotaNotice}
                </p>
              )}
            </div>
          </div>

          <button
            type="button"
            onClick={handleClose}
            aria-label="关闭"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gray-50 text-gray-400 transition-colors hover:bg-gray-200 hover:text-gray-700 focus-visible:outline-none focus-visible:ring-2"
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>

        {/* 底部按钮栏 */}
        <div className="mt-4 flex items-center justify-end gap-2.5 border-t border-gray-100 pt-3.5">
          <button
            type="button"
            onClick={handleClose}
            className="rounded-full px-4 py-1.5 text-xs font-bold text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-800 focus-visible:outline-none focus-visible:ring-2"
          >
            关闭
          </button>

          <button
            type="button"
            onClick={handleAction}
            className={`flex items-center gap-1.5 rounded-full px-5 py-1.5 text-xs font-bold text-white shadow-sm transition-all hover:scale-105 focus-visible:outline-none focus-visible:ring-2 ${c.btn}`}
          >
            {((isReady || isNoRoute) && activeTask.resultRecordId) ? (
              <>
                <span>查看结果</span>
                <ArrowRight size={10} aria-hidden="true" />
              </>
            ) : (
              <span>重新规划</span>
            )}
          </button>
        </div>
      </div>
    </aside>
  );
}
