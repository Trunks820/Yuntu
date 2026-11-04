import type { StoredTripTask } from "@/stores/tripTaskStore";

/**
 * 首页续填卡片（原型 .resume）的状态。
 * 草稿进度优先：用户开始新草稿后，卡片回到「上次的心动」。
 * 没有草稿时跟随最近一次任务，任务状态只取自服务端轮询结果（tripTaskStore），前端不推断。
 */
export type MobileHomeCard =
  | { kind: "draft" }
  | { kind: "generating"; task: StoredTripTask }
  | { kind: "ready"; task: StoredTripTask }
  | { kind: "failed"; task: StoredTripTask }
  | null;

export function selectMobileHomeCard(
  hasDraftProgress: boolean,
  tasks: Record<string, StoredTripTask>,
): MobileHomeCard {
  if (hasDraftProgress) return { kind: "draft" };

  const latest = Object.values(tasks).sort((a, b) => b.startedAt - a.startedAt)[0];
  if (!latest) return null;

  // timeout 只是本地观察暂停，不是服务端结论，仍交给等待页核实
  if (latest.status === "pending" || latest.status === "timeout") {
    return { kind: "generating", task: latest };
  }
  // 已完成的任务打开过一次（acknowledged）后不再占据首页
  if (latest.notificationState === "acknowledged") return null;
  if (latest.status === "ready" && latest.resultRecordId) {
    return { kind: "ready", task: latest };
  }
  return { kind: "failed", task: latest };
}

/** 已完成任务去详情页；生成中或失败的任务去等待页（等待页已有重试入口） */
export function mobileHomeCardTarget(task: StoredTripTask): string {
  if (task.status === "ready" && task.resultRecordId) {
    return `/result/${task.resultRecordId}?job_id=${task.jobId}`;
  }
  return `/planning/${task.jobId}`;
}


