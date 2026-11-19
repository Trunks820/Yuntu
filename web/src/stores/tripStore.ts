import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import type { TripSubmitRequest } from "@/types/tripRequest";
import type { JobStatus, StageProgress, TripResult } from "@/types/trip";

// 结果绑定 resultId+jobId 身份，页面据此判断 store 缓存是否属于当前 URL，
// 避免上一条 job 的结果污染下一条（见 ResultPage/PlanDetailPage 的 matched 守卫）
export interface StoredResult {
  resultId: string;
  jobId: string;
  data: TripResult;
}

interface TripStore {
  formData: TripSubmitRequest | null;
  setFormData: (data: TripSubmitRequest) => void;

  currentJobId: string | null;
  jobStatus: JobStatus | null;
  stageProgress: StageProgress | null;
  setJob: (
    jobId: string,
    status: JobStatus,
    progress: StageProgress | null,
  ) => void;

  result: StoredResult | null;
  setResult: (resultId: string, jobId: string, data: TripResult) => void;
  clearResult: () => void;

  selectedPlanId: string | null;
  selectedDay: number;
  selectPlan: (planId: string) => void;
  selectDay: (day: number) => void;

  reset: () => void;
}

const initialState = {
  formData: null,
  currentJobId: null,
  jobStatus: null,
  stageProgress: null,
  result: null,
  selectedPlanId: null,
  selectedDay: 1,
};

export const useTripStore = create<TripStore>()(
  persist(
    (set) => ({
      ...initialState,

      setFormData: (data) => set({ formData: data }),

      setJob: (jobId, status, progress) =>
        set({ currentJobId: jobId, jobStatus: status, stageProgress: progress }),

      setResult: (resultId, jobId, data) =>
        set({ result: { resultId, jobId, data } }),

      clearResult: () => set({ result: null }),

      selectPlan: (planId) => set({ selectedPlanId: planId, selectedDay: 1 }),

      selectDay: (day) => set({ selectedDay: day }),

      reset: () => set(initialState),
    }),
    {
      name: "yuntu-trip",
      storage: createJSONStorage(() => sessionStorage),
      // 只持久化表单与当前 job —— 让刷新 Loading/详情页不丢偏好；
      // 结果数据走 URL(resultId+job_id) 重新拉取，不必落盘
      partialize: (s) => ({ formData: s.formData, currentJobId: s.currentJobId }),
    },
  ),
);

