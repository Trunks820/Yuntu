import { create } from "zustand";
import { pollJobStatus, ApiRequestError } from "@/services/api";
import { recordGuestJobStatus } from "@/utils/guestTripHistory";
import type { PublicGenerationDelivery } from "@/types/trip";

const LOCAL_STORAGE_KEY = "yuntu_trip_generation_tasks";
const POLL_INTERVAL_MS = 3000;

export type TripTaskStatus = "pending" | "ready" | "failed" | "timeout" | "no_route";
export type TaskNotificationState = "none" | "unread" | "acknowledged";
export type PollingPauseReason = "consecutive_errors" | "timeout" | null;

export interface TaskPollingBudget {
  jobId: string;
  startedAt: number;
  consecutiveErrors: number;
  isPaused: boolean;
  pauseReason: PollingPauseReason;
  activeOwner: string | null;
}

export const MAX_POLLING_DURATION_MS = 360_000; // 默认最长 6 分钟
export const CONSECUTIVE_ERROR_LIMIT = 3; // 连续 3 次传输错误暂停观察

export interface StoredTripTask {
  accountId: string;
  jobId: string;
  requestId?: string | null;
  startedAt: number;
  finishedAt?: number | null;
  status: TripTaskStatus;
  resultRecordId?: string | number | null;
  destination: string;
  notificationState: TaskNotificationState;
  error?: { code: string; message: string } | null;
  /** v0.12: BFF 公共交付投影 */
  delivery?: PublicGenerationDelivery | null;
  /** v0.12: 生成策略版本 */
  generationPolicyVersion?: string | null;
}

interface TripTaskStoreState {
  tasks: Record<string, StoredTripTask>;
  budgets: Record<string, TaskPollingBudget>;
  activePolling: boolean;
  initStore: () => void;
  clearAllTasks: () => void;
  getTask: (jobId: string) => StoredTripTask | undefined;
  getBudget: (jobId: string) => TaskPollingBudget;
  claimTaskOwner: (jobId: string, owner: string) => boolean;
  releaseTaskOwner: (jobId: string, owner: string) => void;
  recordTaskError: (jobId: string, count?: number) => void;
  recordTaskSuccess: (jobId: string) => void;
  checkTaskTimeout: (jobId: string) => boolean;
  restartTaskPolling: (jobId: string) => Promise<unknown>;
  addOrUpdateTask: (task: Partial<StoredTripTask> & { jobId: string }) => void;
  acknowledgeTask: (jobId: string) => void;
  checkAllActiveTasks: () => Promise<void>;
  ensurePollingLoop: () => void;
}


let pollingTimer: ReturnType<typeof setInterval> | null = null;
let initialized = false;
let checkInFlight = false;
let currentEpoch = 0;

function getCurrentAccountId(): string {
  return "guest";
}

function loadFromLocalStorage(): Record<string, StoredTripTask> {
  try {
    const raw = localStorage.getItem(LOCAL_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (parsed && typeof parsed === "object") {
      const cleaned: Record<string, StoredTripTask> = {};
      const currentAcc = getCurrentAccountId();
      for (const [k, v] of Object.entries(parsed)) {
        if (v && typeof v === "object" && "jobId" in v && v.jobId) {
          const item = v as Record<string, unknown>;
          const accId = String(item.accountId || currentAcc);
          if (accId !== currentAcc && accId !== "anonymous") continue;

          let notifState: TaskNotificationState = "none";
          if (
            item.notificationState === "unread" ||
            item.notificationState === "acknowledged" ||
            item.notificationState === "none"
          ) {
            notifState = item.notificationState as TaskNotificationState;
          } else if (
            item.status === "ready" ||
            item.status === "failed" ||
            item.status === "timeout" ||
            item.status === "no_route"
          ) {
            notifState = "unread";
          }

          cleaned[k] = {
            accountId: accId,
            jobId: String(item.jobId),
            requestId: (item.requestId as string | null) ?? null,
            startedAt: (item.startedAt as number) || Date.now(),
            finishedAt: (item.finishedAt as number | null) ?? null,
            status: (item.status as TripTaskStatus) || "pending",
            resultRecordId: (item.resultRecordId as string | number | null) ?? null,
            destination: (item.destination as string) || "目的地",
            notificationState: notifState,
            error: (item.error as { code: string; message: string } | null) ?? null,
            delivery: (item.delivery as PublicGenerationDelivery | null) ?? null,
            generationPolicyVersion: (item.generationPolicyVersion as string | null) ?? null,
          };
        }
      }
      return cleaned;
    }
    return {};
  } catch {
    try {
      localStorage.removeItem(LOCAL_STORAGE_KEY);
    } catch {
      /* ignore */
    }
    return {};
  }
}

function saveToLocalStorage(tasks: Record<string, StoredTripTask>) {
  try {
    const serializable: Record<string, StoredTripTask> = {};
    for (const [k, v] of Object.entries(tasks)) {
      serializable[k] = {
        accountId: v.accountId,
        jobId: v.jobId,
        requestId: v.requestId ?? null,
        startedAt: v.startedAt,
        finishedAt: v.finishedAt ?? null,
        status: v.status,
        resultRecordId: v.resultRecordId ?? null,
        destination: v.destination,
        notificationState: v.notificationState || "none",
        error: v.error ?? null,
        delivery: v.delivery ?? null,
        generationPolicyVersion: v.generationPolicyVersion ?? null,
      };
    }
    localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(serializable));
  } catch {
    /* ignore */
  }
}

export const useTripTaskStore = create<TripTaskStoreState>((set, get) => ({
  tasks: {},
  budgets: {},
  activePolling: false,

  initStore: () => {
    if (initialized) return;
    initialized = true;

    const loaded = loadFromLocalStorage();
    set({ tasks: { ...loaded, ...get().tasks } });

    const hasActive = Object.values(loaded).some((t) => t.status === "pending");
    if (hasActive) {
      void get().checkAllActiveTasks();
      get().ensurePollingLoop();
    }
  },

  clearAllTasks: () => {
    initialized = false;
    currentEpoch++;
    if (pollingTimer) {
      clearInterval(pollingTimer);
      pollingTimer = null;
    }
    try {
      localStorage.removeItem(LOCAL_STORAGE_KEY);
    } catch {
      /* ignore */
    }
    set({ tasks: {}, budgets: {}, activePolling: false });
  },

  getTask: (jobId: string) => {
    return get().tasks[jobId];
  },

  getBudget: (jobId: string): TaskPollingBudget => {
    const existing = get().budgets[jobId];
    if (existing) return existing;
    const initial: TaskPollingBudget = {
      jobId,
      startedAt: Date.now(),
      consecutiveErrors: 0,
      isPaused: false,
      pauseReason: null,
      activeOwner: null,
    };
    set((s) => ({ budgets: { ...s.budgets, [jobId]: initial } }));
    return initial;
  },

  claimTaskOwner: (jobId: string, owner: string): boolean => {
    const budget = get().getBudget(jobId);
    set((s) => ({
      budgets: {
        ...s.budgets,
        [jobId]: { ...budget, activeOwner: owner },
      },
    }));
    return true;
  },

  releaseTaskOwner: (jobId: string, owner: string): void => {
    const budget = get().budgets[jobId];
    if (budget && budget.activeOwner === owner) {
      set((s) => ({
        budgets: {
          ...s.budgets,
          [jobId]: { ...budget, activeOwner: null },
        },
      }));
    }
  },

  recordTaskError: (jobId: string, count?: number): void => {
    const budget = get().getBudget(jobId);
    const nextErrors = typeof count === "number" ? count : budget.consecutiveErrors + 1;
    const isPaused = nextErrors >= CONSECUTIVE_ERROR_LIMIT;
    set((s) => ({
      budgets: {
        ...s.budgets,
        [jobId]: {
          ...budget,
          consecutiveErrors: nextErrors,
          isPaused: isPaused ? true : budget.isPaused,
          pauseReason: isPaused ? "consecutive_errors" : budget.pauseReason,
        },
      },
    }));
  },

  recordTaskSuccess: (jobId: string): void => {
    const budget = get().getBudget(jobId);
    if (budget.consecutiveErrors > 0) {
      set((s) => ({
        budgets: {
          ...s.budgets,
          [jobId]: { ...budget, consecutiveErrors: 0 },
        },
      }));
    }
  },

  checkTaskTimeout: (jobId: string): boolean => {
    const budget = get().getBudget(jobId);
    const task = get().tasks[jobId];
    if (task && task.status === "pending") {
      if (Date.now() - budget.startedAt >= MAX_POLLING_DURATION_MS) {
        set((s) => ({
          budgets: {
            ...s.budgets,
            [jobId]: {
              ...budget,
              isPaused: true,
              pauseReason: "timeout",
            },
          },
        }));
        return true;
      }
    }
    return false;
  },

  restartTaskPolling: async (jobId: string) => {
    const current = get().getBudget(jobId);
    const refreshed: TaskPollingBudget = {
      ...current,
      startedAt: Date.now(),
      consecutiveErrors: 0,
      isPaused: false,
      pauseReason: null,
    };
    set((s) => ({
      budgets: {
        ...s.budgets,
        [jobId]: refreshed,
      },
    }));

    try {
      const res = await pollJobStatus(jobId);
      get().recordTaskSuccess(jobId);

      const isNoRoute = res.delivery?.outcome === "NO_ROUTE";
      if ((res.status === "COMPLETED" && res.result_record_id) || isNoRoute) {
        set((s) => {
          const cur = s.tasks[jobId];
          const targetStatus: TripTaskStatus = isNoRoute ? "no_route" : "ready";
          if (!cur) return s;
          const updated: StoredTripTask = {
            ...cur,
            status: targetStatus,
            resultRecordId: res.result_record_id,
            finishedAt: cur.finishedAt || Date.now(),
            notificationState: cur.notificationState === "none" ? "unread" : cur.notificationState,
            delivery: res.delivery ?? null,
            generationPolicyVersion: res.generation_policy_version ?? cur.generationPolicyVersion ?? null,
          };
          const next = { ...s.tasks, [jobId]: updated };
          saveToLocalStorage(next);
          return { tasks: next };
        });
      } else if (res.status === "FAILED") {
        set((s) => {
          const cur = s.tasks[jobId];
          if (!cur) return s;
          const updated: StoredTripTask = {
            ...cur,
            status: "failed",
            finishedAt: cur.finishedAt || Date.now(),
            error: { code: "GENERATION_FAILED", message: "生成失败，请重试" },
            notificationState: cur.notificationState === "none" ? "unread" : cur.notificationState,
            delivery: res.delivery ?? null,
            generationPolicyVersion: res.generation_policy_version ?? cur.generationPolicyVersion ?? null,
          };
          const next = { ...s.tasks, [jobId]: updated };
          saveToLocalStorage(next);
          return { tasks: next };
        });
      }
      get().ensurePollingLoop();
      return res;
    } catch {
      get().recordTaskError(jobId);
      return null;
    }
  },

  addOrUpdateTask: (item) => {
    set((s) => {
      const existing = s.tasks[item.jobId];
      const accId = item.accountId || existing?.accountId || getCurrentAccountId();
      const updated: StoredTripTask = {
        accountId: accId,
        jobId: item.jobId,
        requestId: item.requestId ?? existing?.requestId ?? null,
        startedAt: item.startedAt ?? existing?.startedAt ?? Date.now(),
        finishedAt: item.finishedAt ?? existing?.finishedAt ?? null,
        status: item.status ?? existing?.status ?? "pending",
        resultRecordId: item.resultRecordId ?? existing?.resultRecordId ?? null,
        destination: item.destination || existing?.destination || "目的地",
        notificationState: item.notificationState ?? existing?.notificationState ?? "none",
        error: item.error ?? existing?.error ?? null,
        delivery: item.delivery ?? existing?.delivery ?? null,
        generationPolicyVersion: item.generationPolicyVersion ?? existing?.generationPolicyVersion ?? null,
      };
      const next = { ...s.tasks, [item.jobId]: updated };
      saveToLocalStorage(next);
      return { tasks: next };
    });
    const finalTask = get().tasks[item.jobId];
    if (finalTask && finalTask.status !== "pending") {
      const mappedStatus =
        finalTask.status === "ready" || finalTask.status === "no_route"
          ? "SUCCESS"
          : finalTask.status === "timeout"
            ? "TIMEOUT"
            : "FAILED";
      recordGuestJobStatus(
        item.jobId,
        {
          status: mappedStatus,
          result_record_id: finalTask.resultRecordId,
          delivery: finalTask.delivery,
          generation_policy_version: finalTask.generationPolicyVersion,
          error: finalTask.error
            ? {
                code: finalTask.error.code,
                message: finalTask.error.message,
                retryable: true,
              }
            : null,
        },
        {
          destination: finalTask.destination,
          startedAt: finalTask.startedAt,
        },
      );
    }
    get().ensurePollingLoop();
  },

  acknowledgeTask: (jobId: string) => {
    set((s) => {
      const task = s.tasks[jobId];
      if (!task || task.notificationState === "acknowledged") return s;
      const updated: StoredTripTask = {
        ...task,
        notificationState: "acknowledged",
      };
      const next = { ...s.tasks, [jobId]: updated };
      saveToLocalStorage(next);
      return { tasks: next };
    });
  },

  ensurePollingLoop: () => {
    const state = get();
    const hasActive = Object.values(state.tasks).some((t) => t.status === "pending");
    const isHidden = typeof document !== "undefined" ? document.hidden : false;

    if (hasActive && !pollingTimer && !isHidden) {
      pollingTimer = setInterval(() => {
        void get().checkAllActiveTasks();
      }, POLL_INTERVAL_MS);
      set({ activePolling: true });
    } else if (!hasActive && pollingTimer) {
      clearInterval(pollingTimer);
      pollingTimer = null;
      set({ activePolling: false });
    }
  },

  checkAllActiveTasks: async () => {
    if (checkInFlight) return;
    checkInFlight = true;
    const requestEpoch = currentEpoch;

    try {
      const state = get();
      const activeKeys = Object.keys(state.tasks).filter(
        (k) => state.tasks[k]?.status === "pending",
      );

      if (activeKeys.length === 0) {
        if (currentEpoch === requestEpoch) get().ensurePollingLoop();
        return;
      }

      for (const jobId of activeKeys) {
        if (currentEpoch !== requestEpoch) break;
        const task = get().tasks[jobId];
        if (!task) continue;

        // 1. 超时预算权威生效：先执行超时检查（无论前台或后台）
        if (get().checkTaskTimeout(jobId)) {
          continue;
        }

        const budget = get().getBudget(jobId);

        // 2. 如果该任务处于观察暂停状态，跳过
        if (budget.isPaused) {
          continue;
        }

        // 3. 如果有其他前台活跃所有者（如 PlanningPage），跳过后台轮询，避免同一 job 重复查询
        if (budget.activeOwner && budget.activeOwner !== "background") {
          continue;
        }

        try {
          const res = await pollJobStatus(jobId);
          if (currentEpoch !== requestEpoch) break;
          get().recordTaskSuccess(jobId);

          const isNoRoute = res.delivery?.outcome === "NO_ROUTE";
          if ((res.status === "COMPLETED" && res.result_record_id) || isNoRoute) {
            set((s) => {
              if (currentEpoch !== requestEpoch) return s;
              const cur = s.tasks[jobId];
              const targetStatus: TripTaskStatus = isNoRoute ? "no_route" : "ready";
              if (!cur || cur.status === targetStatus) return s;

              const updated: StoredTripTask = {
                ...cur,
                status: targetStatus,
                resultRecordId: res.result_record_id,
                finishedAt: cur.finishedAt || Date.now(),
                notificationState:
                  cur.notificationState === "none" ? "unread" : cur.notificationState,
                delivery: res.delivery ?? null,
                generationPolicyVersion: res.generation_policy_version ?? cur.generationPolicyVersion ?? null,
              };
              const next = { ...s.tasks, [jobId]: updated };
              saveToLocalStorage(next);
              return { tasks: next };
            });
          } else if (res.status === "FAILED") {
            set((s) => {
              if (currentEpoch !== requestEpoch) return s;
              const cur = s.tasks[jobId];
              if (!cur || cur.status === "failed") return s;

              const updated: StoredTripTask = {
                ...cur,
                status: "failed",
                finishedAt: cur.finishedAt || Date.now(),
                error: { code: "GENERATION_FAILED", message: "生成失败，请重试" },
                notificationState:
                  cur.notificationState === "none" ? "unread" : cur.notificationState,
                delivery: res.delivery ?? null,
                generationPolicyVersion: res.generation_policy_version ?? cur.generationPolicyVersion ?? null,
              };
              const next = { ...s.tasks, [jobId]: updated };
              saveToLocalStorage(next);
              return { tasks: next };
            });
          }
        } catch (err) {
          if (
            err instanceof ApiRequestError &&
            (err.status === 401 || err.code === "AUTH_REQUIRED")
          ) {
            break;
          }
          // 传输错误：记录错误计数，达到 3 次时暂停观察，不篡改服务端 task.status
          get().recordTaskError(jobId);
        }
      }
    } finally {
      checkInFlight = false;
      if (currentEpoch === requestEpoch) {
        get().ensurePollingLoop();
      }
    }
  },
}));

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (pollingTimer) {
        clearInterval(pollingTimer);
        pollingTimer = null;
        useTripTaskStore.setState({ activePolling: false });
      }
    } else {
      useTripTaskStore.getState().ensurePollingLoop();
    }
  });
}
