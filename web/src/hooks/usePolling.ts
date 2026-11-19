import { useEffect, useRef, useCallback } from "react";

interface UsePollingOptions<T> {
  fetcher: () => Promise<T>;
  onData: (data: T) => boolean | void;
  interval?: number;
  maxAttempts?: number;
  deadline?: number;
  timeoutMs?: number;
  initialConsecutiveErrors?: number;
  onTimeout?: () => void;
  onConsecutiveErrors?: (count: number) => void;
  consecutiveErrorThreshold?: number;
  enabled?: boolean;
}

export function usePolling<T>({
  fetcher,
  onData,
  interval = 2000,
  maxAttempts = 90,
  deadline,
  timeoutMs: _timeoutMs,
  initialConsecutiveErrors = 0,
  onTimeout,
  onConsecutiveErrors,
  consecutiveErrorThreshold: _threshold = 3,
  enabled = true,
}: UsePollingOptions<T>) {
  const attemptRef = useRef(0);
  const consecutiveErrorsRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const stoppedRef = useRef(false);
  const inFlightRef = useRef(false);
  const roundIdRef = useRef(0);
  const requestSeqRef = useRef(0);
  const waitingForWakeupRef = useRef(false);
  const activeTickRef = useRef<(() => Promise<void>) | null>(null);

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const onDataRef = useRef(onData);
  onDataRef.current = onData;
  const onTimeoutRef = useRef(onTimeout);
  onTimeoutRef.current = onTimeout;
  const onConsecutiveErrorsRef = useRef(onConsecutiveErrors);
  onConsecutiveErrorsRef.current = onConsecutiveErrors;
  const maxAttemptsRef = useRef(maxAttempts);
  maxAttemptsRef.current = maxAttempts;
  const deadlineRef = useRef(deadline);
  deadlineRef.current = deadline;

  useEffect(() => {
    if (initialConsecutiveErrors > 0 && consecutiveErrorsRef.current === 0) {
      consecutiveErrorsRef.current = initialConsecutiveErrors;
    }
  }, [initialConsecutiveErrors]);

  const stop = useCallback(() => {
    stoppedRef.current = true;
    roundIdRef.current++;
    waitingForWakeupRef.current = false;
    activeTickRef.current = null;
    clearTimeout(timerRef.current);
  }, []);

  useEffect(() => {
    if (!enabled) return;

    const roundId = ++roundIdRef.current;
    stoppedRef.current = false;
    attemptRef.current = 0;
    consecutiveErrorsRef.current = initialConsecutiveErrors;
    waitingForWakeupRef.current = false;

    async function tick() {
      if (stoppedRef.current || roundId !== roundIdRef.current) return;
      // 严格真正在途互斥：任何时候都不允许并发两个 poll GET
      if (inFlightRef.current) {
        // 如果当前已有在途请求（例如前一轮遗留的慢请求），登记当前活动轮次需要唤醒，切勿丢弃轮次
        waitingForWakeupRef.current = true;
        return;
      }
      waitingForWakeupRef.current = false;

      const currentDeadline = deadlineRef.current;
      if (currentDeadline && Date.now() >= currentDeadline) {
        stoppedRef.current = true;
        onTimeoutRef.current?.();
        return;
      }

      const limit = maxAttemptsRef.current ?? 90;
      if (attemptRef.current >= limit) {
        stoppedRef.current = true;
        onTimeoutRef.current?.();
        return;
      }

      const currentSeq = ++requestSeqRef.current;
      inFlightRef.current = true;
      attemptRef.current++;

      const isStale = () =>
        stoppedRef.current ||
        roundId !== roundIdRef.current ||
        currentSeq !== requestSeqRef.current;

      try {
        const data = await fetcherRef.current();
        if (isStale()) return;
        if (consecutiveErrorsRef.current > 0) {
          consecutiveErrorsRef.current = 0;
          onConsecutiveErrorsRef.current?.(0);
        }
        const shouldStop = onDataRef.current(data);
        if (shouldStop) {
          stoppedRef.current = true;
          return;
        }
      } catch {
        if (isStale()) return;
        consecutiveErrorsRef.current++;
        onConsecutiveErrorsRef.current?.(consecutiveErrorsRef.current);
      } finally {
        inFlightRef.current = false;
        // 关键唤醒逻辑：当前在途请求已彻底沉降并释放互斥锁
        // 若当前请求已属于过时旧轮次，且最新活动轮次正处于等待唤醒状态，安排恰好一次活动轮次唤醒！
        if (
          isStale() &&
          !stoppedRef.current &&
          waitingForWakeupRef.current &&
          activeTickRef.current
        ) {
          waitingForWakeupRef.current = false;
          clearTimeout(timerRef.current);
          void activeTickRef.current();
        }
      }

      if (isStale()) return;

      const latestDeadline = deadlineRef.current;
      if (latestDeadline && Date.now() >= latestDeadline) {
        stoppedRef.current = true;
        onTimeoutRef.current?.();
        return;
      }

      if (attemptRef.current >= (maxAttemptsRef.current ?? 90)) {
        stoppedRef.current = true;
        onTimeoutRef.current?.();
        return;
      }

      if (document.hidden) return;

      timerRef.current = setTimeout(tick, interval);
    }

    activeTickRef.current = tick;

    function handleVisibilityChange() {
      if (document.hidden) {
        clearTimeout(timerRef.current);
      } else if (
        !stoppedRef.current &&
        roundId === roundIdRef.current &&
        !inFlightRef.current &&
        attemptRef.current < (maxAttemptsRef.current ?? 90)
      ) {
        clearTimeout(timerRef.current);
        void tick();
      }
    }

    document.addEventListener("visibilitychange", handleVisibilityChange);
    void tick();

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      stoppedRef.current = true;
      waitingForWakeupRef.current = false;
      activeTickRef.current = null;
      clearTimeout(timerRef.current);
    };
    // initialConsecutiveErrors 等故意不作为重跑依赖，由内部 round 与 ref 安全维持
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, interval, deadline]);

  return { stop };
}
