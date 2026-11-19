import { useEffect, useRef, useCallback, useState } from "react";
import { useJobStream } from "./useJobStream";
import { usePolling } from "./usePolling";
import { pollJobStatus } from "@/services/api";
import type { JobResponse } from "@/types/trip";

const SSE_DISABLED = import.meta.env.VITE_JOB_STREAM === "off";
const FALLBACK_WINDOW_MS = 8_000;
const FALLBACK_ERROR_THRESHOLD = 3;

interface UseJobProgressOptions {
  jobId: string | undefined;
  onData: (data: JobResponse) => boolean | void;
  onTimeout?: () => void;
  onConsecutiveErrors?: (count: number) => void;
  consecutiveErrorThreshold?: number;
  enabled?: boolean;
  timeoutMs?: number;
  deadline?: number;
  initialConsecutiveErrors?: number;
}

export function useJobProgress({
  jobId,
  onData,
  onTimeout,
  onConsecutiveErrors,
  consecutiveErrorThreshold: _threshold = 3,
  enabled = true,
  timeoutMs = 360_000,
  deadline,
  initialConsecutiveErrors = 0,
}: UseJobProgressOptions) {
  const [degraded, setDegraded] = useState(SSE_DISABLED);
  const startTimeRef = useRef(0);
  const consecutiveErrorsRef = useRef(initialConsecutiveErrors);
  const fallbackDeadlineRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    consecutiveErrorsRef.current = initialConsecutiveErrors;
  }, [initialConsecutiveErrors]);

  useEffect(() => {
    if (enabled && jobId) {
      startTimeRef.current = Date.now();
      fallbackDeadlineRef.current = undefined;
    }
  }, [enabled, jobId]);

  if (!deadline && timeoutMs && !fallbackDeadlineRef.current) {
    fallbackDeadlineRef.current = Date.now() + timeoutMs;
  }
  const effectiveDeadline = deadline ?? fallbackDeadlineRef.current;

  const handleSSEErrors = useCallback(
    (count: number) => {
      consecutiveErrorsRef.current = count;
      onConsecutiveErrors?.(count);
      if (
        count >= FALLBACK_ERROR_THRESHOLD &&
        Date.now() - startTimeRef.current < FALLBACK_WINDOW_MS
      ) {
        console.warn(
          "[useJobProgress] SSE endpoint unavailable, falling back to polling",
        );
        setDegraded(true);
      }
    },
    [onConsecutiveErrors],
  );

  const handlePollingErrors = useCallback(
    (count: number) => {
      consecutiveErrorsRef.current = count;
      onConsecutiveErrors?.(count);
    },
    [onConsecutiveErrors],
  );

  const handleData = useCallback(
    (data: JobResponse) => {
      consecutiveErrorsRef.current = 0;
      onConsecutiveErrors?.(0);
      return onData(data);
    },
    [onData, onConsecutiveErrors],
  );

  const handleSSEInterrupted = useCallback(() => {
    console.warn(
      "[useJobProgress] SSE interrupted by server, immediately switching to polling",
    );
    setDegraded(true);
  }, []);

  const { stop: stopSSE } = useJobStream({
    jobId,
    onData: handleData,
    onTimeout,
    onConsecutiveErrors: handleSSEErrors,
    onInterrupted: handleSSEInterrupted,
    consecutiveErrorThreshold: FALLBACK_ERROR_THRESHOLD,
    timeoutMs,
    deadline: effectiveDeadline,
    initialConsecutiveErrors: consecutiveErrorsRef.current,
    enabled: enabled && !degraded,
  });

  const fetcher = useCallback(() => pollJobStatus(jobId!), [jobId]);

  // 基于稳定生命周期计算安全最大尝试上限，避免每次普通渲染时由 Date.now() 波动重新计算导致副作用重置
  const initialBudgetMs = effectiveDeadline && startTimeRef.current
    ? Math.max(0, effectiveDeadline - startTimeRef.current)
    : timeoutMs;
  const maxAttempts = Math.max(1, Math.ceil(initialBudgetMs / 2500));

  const { stop: stopPolling } = usePolling({
    fetcher,
    onData: handleData,
    interval: 2500,
    maxAttempts,
    deadline: effectiveDeadline,
    timeoutMs: initialBudgetMs,
    initialConsecutiveErrors: consecutiveErrorsRef.current,
    onTimeout,
    onConsecutiveErrors: handlePollingErrors,
    enabled: enabled && degraded && !!jobId,
  });

  const stop = useCallback(() => {
    stopSSE();
    stopPolling();
  }, [stopSSE, stopPolling]);

  return { stop };
}
