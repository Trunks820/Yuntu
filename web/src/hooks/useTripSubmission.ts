import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { submitTrip } from "@/services/api";
import { showToast } from "@/stores/toastStore";
import { useTripStore } from "@/stores/tripStore";
import { useTripTaskStore } from "@/stores/tripTaskStore";
import type { TripSubmitRequest } from "@/types/tripRequest";
import {
  clearCinematicSubmission,
  saveCinematicSubmission,
} from "@/utils/cinematicSubmission";
import {
  clearMobileSubmission,
  generateFingerprint,
  getMobileSubmission,
  hasBlockingUnknownSubmission,
  isValidRequestId,
  isMobileSubmissionIntact,
  markSubmissionConfirmed,
  saveMobileSubmission,
} from "@/utils/mobileSubmission";

export interface UseTripSubmissionOptions {
  source: "cinematic" | "mobile";
  onRequestPrepare: () =>
    | Promise<TripSubmitRequest | null>
    | TripSubmitRequest
    | null;
  onRequireLogin?: (intent: string) => void;
  onSuccess?: (jobId: string, requestId: string) => void;
  onError?: (error: unknown) => void;
}

export interface SubmitOptions {
  isSameRetry?: boolean;
}

const GUEST_OWNER_ID = "guest";

/**
 * 跨端提交窄协调器 (useTripSubmission) - 开源免登录模式
 */
export function useTripSubmission({
  source,
  onRequestPrepare,
  onSuccess,
  onError,
}: UseTripSubmissionOptions) {
  const navigate = useNavigate();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const submissionLock = useRef(false);
  const submissionIdRef = useRef<number>(0);

  useEffect(() => {
    const submissionEpoch = submissionIdRef;
    const lock = submissionLock;
    return () => {
      submissionEpoch.current++;
      lock.current = false;
    };
  }, []);

  const executeSubmit = useCallback(
    async (options?: SubmitOptions): Promise<boolean> => {
      const isSameRetry = Boolean(options?.isSameRetry);

      if (submissionLock.current) {
        return false;
      }

      submissionLock.current = true;
      setIsSubmitting(true);
      const currentSubmissionId = ++submissionIdRef.current;

      const stillCurrent = () =>
        submissionIdRef.current === currentSubmissionId;

      try {
        let requestToSend: TripSubmitRequest;
        let requestId: string;

        if (isSameRetry) {
          const frozen =
            source === "mobile" ? getMobileSubmission(GUEST_OWNER_ID) : null;

          if (
            !frozen ||
            frozen.owner !== GUEST_OWNER_ID ||
            !isValidRequestId(frozen.request_id) ||
            !isMobileSubmissionIntact(frozen)
          ) {
            showToast("原提交记录缺失或完整性异常，请先核查原请求状态", "error");
            return false;
          }

          requestToSend = frozen.trip_request;
          requestId = frozen.request_id;
        } else {
          const request = await onRequestPrepare();
          if (!stillCurrent() || !request) {
            return false;
          }

          if (source === "mobile") {
            const fingerprint = generateFingerprint(request);
            if (hasBlockingUnknownSubmission(GUEST_OWNER_ID, fingerprint)) {
              showToast(
                "有尚未确认状态的提交，请先确认原行程状态",
                "error",
              );
              return false;
            }

            const submission = saveMobileSubmission(request, GUEST_OWNER_ID);
            requestId = submission.request_id;
          } else {
            const submission = saveCinematicSubmission(request, GUEST_OWNER_ID);
            requestId = submission.request_id;
          }

          requestToSend = request;
        }

        if (!stillCurrent()) return false;

        const submitRes = await submitTrip(requestToSend, requestId);

        if (!stillCurrent()) return false;

        if (submitRes.job_id) {
          useTripStore.getState().setFormData(requestToSend);
          useTripStore.getState().clearResult();
          useTripTaskStore.getState().addOrUpdateTask({
            jobId: submitRes.job_id,
            requestId,
            destination: requestToSend.to_city,
            startedAt: Date.now(),
            status: "pending",
            notificationState: "none",
          });
          useTripStore.getState().setJob(submitRes.job_id, "QUEUED", null);

          if (source === "mobile") {
            markSubmissionConfirmed(submitRes.job_id, GUEST_OWNER_ID);
            clearMobileSubmission(GUEST_OWNER_ID);
          } else {
            clearCinematicSubmission();
          }

          if (stillCurrent()) {
            navigate(`/planning/${submitRes.job_id}`);
            onSuccess?.(submitRes.job_id, requestId);
          }
          return true;
        }

        return false;
      } catch (err: unknown) {
        if (!stillCurrent()) return false;

        const msg =
          err instanceof Error ? err.message : "提交失败，请稍后重试";
        showToast(msg, "error");
        onError?.(err);
        return false;
      } finally {
        if (submissionIdRef.current === currentSubmissionId) {
          submissionLock.current = false;
        }
        setIsSubmitting(false);
      }
    },
    [
      source,
      onRequestPrepare,
      onSuccess,
      onError,
      navigate,
    ],
  );

  return {
    isSubmitting,
    executeSubmit,
  };
}
