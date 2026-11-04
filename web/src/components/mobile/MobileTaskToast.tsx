import React, { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  useTripTaskStore,
  type StoredTripTask,
  type TripTaskStatus,
} from "@/stores/tripTaskStore";
import "@/styles/mobile.css";

/** 原型 runGeneration：完成时不在等待页就弹成功句；失败句按同一语气补齐 */
const TOAST_COPY = {
  ready: "旅行册已装订好，回首页就能翻开。",
  failed: "这次没能成册，回首页看看原因",
} as const;

/** 已提示过、或用户已亲眼看到结果的任务；存下来，刷新后也不再弹 */
const NOTIFIED_STORAGE_KEY = "yuntu_mobile_task_toasts";
const NOTIFIED_LIMIT = 50;
const TOAST_MS = 2400;

function isInProgress(status: TripTaskStatus): boolean {
  return status === "pending" || status === "timeout";
}

function loadNotified(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(NOTIFIED_STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed)
      ? parsed.filter((id): id is string => typeof id === "string")
      : [];
  } catch {
    return [];
  }
}

function markNotified(jobId: string): void {
  const ids = loadNotified();
  if (ids.includes(jobId)) return;
  try {
    localStorage.setItem(
      NOTIFIED_STORAGE_KEY,
      JSON.stringify([...ids, jobId].slice(-NOTIFIED_LIMIT)),
    );
  } catch {
    // 存储不可用时不影响本次提示
  }
}

/** 与首页卡片同一口径：有结果的 ready 算成册，其余结束态都算没能成册 */
function toastCopy(task: StoredTripTask): string {
  return task.status === "ready" && task.resultRecordId ? TOAST_COPY.ready : TOAST_COPY.failed;
}

/**
 * 手机视口的生成完成提示，替代桌面通知卡片。
 * 只提示、不调用 acknowledgeTask：首页卡片保留到用户点开。
 */
export const MobileTaskToast: React.FC = () => {
  const location = useLocation();
  const pathnameRef = useRef(location.pathname);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    pathnameRef.current = location.pathname;
    // 等待页拿到结果会带着 job_id 跳到结果页，而任务状态要等后台轮询才更新；用户已经看到，之后不再提示
    const jobId = new URLSearchParams(location.search).get("job_id");
    if (jobId && /^\/(result|plan)\//.test(location.pathname)) markNotified(jobId);
  }, [location.pathname, location.search]);

  useEffect(() => {
    let timer: number | undefined;
    const unsubscribe = useTripTaskStore.subscribe((state, prev) => {
      for (const task of Object.values(state.tasks)) {
        const before = prev.tasks[task.jobId];
        if (!before || !isInProgress(before.status) || isInProgress(task.status)) continue;
        if (task.notificationState === "acknowledged" || loadNotified().includes(task.jobId)) {
          continue;
        }
        markNotified(task.jobId);
        // 在它自己的等待页上结束时，页面会自己跳转或给出原因
        if (pathnameRef.current === `/planning/${task.jobId}`) continue;
        setMessage(toastCopy(task));
        window.clearTimeout(timer);
        timer = window.setTimeout(() => setMessage(null), TOAST_MS);
      }
    });
    return () => {
      unsubscribe();
      window.clearTimeout(timer);
    };
  }, []);

  if (!message) return null;
  return (
    <div className="ym ym-toast-layer">
      <div className="toast" role="status" aria-live="polite">
        {message}
      </div>
    </div>
  );
};

export default MobileTaskToast;


