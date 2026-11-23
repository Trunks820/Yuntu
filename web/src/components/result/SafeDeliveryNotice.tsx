import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { submitTrip, ApiRequestError } from "@/services/api";
import { useTripStore } from "@/stores/tripStore";
import { useTripTaskStore } from "@/stores/tripTaskStore";
import type { TripSubmitRequest } from "@/types/tripRequest";
import type { TripResult } from "@/types/trip";
import {
  clearPendingSubmission,
  getPendingSubmission,
  savePendingSubmission,
} from "@/utils/pendingSubmission";

function shouldShowSafeDeliveryNotice(
  result: Pick<TripResult, "published_variant" | "delivery_status" | "delivery">,
): boolean {
  if (result.delivery) {
    return result.delivery.outcome === "BASIC";
  }
  return (
    result.published_variant === "safe" && result.delivery_status === "DEGRADED"
  );
}

function findRetryInput(): TripSubmitRequest | null {
  return (
    useTripStore.getState().formData ??
    getPendingSubmission()?.trip_request ??
    null
  );
}

interface SafeDeliveryNoticeProps {
  result: Pick<TripResult, "published_variant" | "delivery_status" | "delivery">;
  jobId?: string | null;
}

export function SafeDeliveryNotice({ result, jobId }: SafeDeliveryNoticeProps) {
  const navigate = useNavigate();
  const setFormData = useTripStore((state) => state.setFormData);
  const clearResult = useTripStore((state) => state.clearResult);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!shouldShowSafeDeliveryNotice(result)) return null;

  const regenerate = async () => {
    if (!jobId || submitting) return;
    setSubmitting(true);
    setError(null);

    try {
      const requestData = findRetryInput();
      if (!requestData) {
        setError("暂时无法读取原行程条件，请回到首页重新填写。");
        return;
      }

      setFormData(requestData);
      clearResult();
      const pending = savePendingSubmission(requestData);

      try {
        const response = await submitTrip(requestData, pending.request_id);
        useTripTaskStore.getState().addOrUpdateTask({
          jobId: response.job_id,
          requestId: pending.request_id,
          destination: requestData.to_city || "目的地",
          startedAt: Date.now(),
          status: "pending",
          notificationState: "none",
        });
        clearPendingSubmission();
        navigate(`/planning/${response.job_id}`);
      } catch (submissionError: unknown) {
        if (submissionError instanceof ApiRequestError) {
          if (
            submissionError.status === 400 ||
            submissionError.status === 422 ||
            submissionError.status === 429 ||
            [
              "REQUEST_ID_CONFLICT",
              "CITY_NOT_SUPPORTED",
              "VALIDATION_ERROR",
            ].includes(submissionError.code)
          ) {
            clearPendingSubmission();
          }
          setError(submissionError.message);
        } else {
          setError("发起重新生成失败，请检查网络设置。");
        }
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section
      aria-label="行程说明"
      className="rounded-2xl border border-amber-200/80 bg-amber-50/90 px-5 py-4 shadow-sm"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm font-medium leading-6 text-amber-950">
          {result.delivery ? "基础行程已生成" : "已生成基础行程，部分详细介绍暂未展开。"}
        </p>
        <button
          type="button"
          disabled={!jobId || submitting}
          onClick={() => void regenerate()}
          className="shrink-0 rounded-full border border-amber-300 bg-white px-4 py-2 text-sm font-bold text-amber-900 transition-colors hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "正在重新生成…" : "重新生成详细版"}
        </button>
      </div>
      {!jobId && (
        <p className="mt-2 text-xs text-amber-800">
          当前链接缺少任务信息，无法直接重新生成。
        </p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-xs text-red-700">
          {error}
        </p>
      )}
    </section>
  );
}
