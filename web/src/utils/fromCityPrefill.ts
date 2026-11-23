import { useCallback, useEffect, useRef, useState } from "react";
import { getHistoryTrips, guessCity } from "@/services/api";

export type AuthStatus = "unknown" | "anonymous" | "authenticated";

export const LAST_FROM_CITY_STORAGE_KEY = "yuntu-last-from-city-v1";
export const GEO_GUESS_HINT_TEXT = "按你的网络位置猜的，可以改";
const DEFAULT_FROM_CITY_MAX_LENGTH = 60;

export type FromCityPrefillSource = "history" | "storage" | "geo";

export interface FromCityPrefillResult {
  city: string;
  source: FromCityPrefillSource;
}

function normalizeCityValue(
  raw: unknown,
  maxLength: number = DEFAULT_FROM_CITY_MAX_LENGTH,
): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  return maxLength > 0 ? trimmed.slice(0, maxLength) : trimmed;
}

/**
 * 读取本设备浏览器上次提交记录的出发城市（localStorage 不可用时静默返回 null）
 */
export function readLastFromCity(
  maxLength: number = DEFAULT_FROM_CITY_MAX_LENGTH,
): string | null {
  try {
    if (typeof localStorage === "undefined") return null;
    const raw = localStorage.getItem(LAST_FROM_CITY_STORAGE_KEY);
    return normalizeCityValue(raw, maxLength);
  } catch {
    return null;
  }
}

/**
 * 提交时将最终出发城市写入本设备浏览器记录（空值跳过，localStorage 不可用时静默忽略）
 */
export function saveLastFromCity(
  city: string | null | undefined,
  maxLength: number = DEFAULT_FROM_CITY_MAX_LENGTH,
): void {
  const normalized = normalizeCityValue(city, maxLength);
  if (!normalized) return;
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(LAST_FROM_CITY_STORAGE_KEY, normalized);
  } catch {
    /* 隐私模式或存储配额受限时静默忽略 */
  }
}

/**
 * 清除本设备浏览器记录的出发城市
 */
export function clearLastFromCity(): void {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.removeItem(LAST_FROM_CITY_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

export interface ResolveFromCityPrefillOptions {
  isAuthenticated: boolean;
  signal?: AbortSignal;
  maxLength?: number;
}

/**
 * 按约定优先级解析出发城市预填值：
 * 1. 登录用户：取历史最近一单（limit=1）的 retry_input.trip_request.from_city（未登录绝不调历史接口）；
 * 2. 本设备浏览器 localStorage 里上次提交的出发城市；
 * 3. 调用 BFF GET /api/geo/guess-city 按网络位置猜测（失败、超时或 null 均静默返回 null）。
 */
export async function resolveFromCityPrefill(
  options: ResolveFromCityPrefillOptions,
): Promise<FromCityPrefillResult | null> {
  const {
    isAuthenticated,
    signal,
    maxLength = DEFAULT_FROM_CITY_MAX_LENGTH,
  } = options;

  if (signal?.aborted) return null;

  // 1. 已登录：尝试取最近一单的出发城市
  if (isAuthenticated) {
    try {
      const history = await getHistoryTrips({ limit: 1 });
      if (signal?.aborted) return null;
      const latestCity = normalizeCityValue(
        history?.items?.[0]?.retry_input?.trip_request?.from_city,
        maxLength,
      );
      if (latestCity) {
        return { city: latestCity, source: "history" };
      }
    } catch {
      if (signal?.aborted) return null;
      // 历史接口失败静默降级到下一步
    }
  }

  // 2. 本设备浏览器上次提交的出发城市
  if (signal?.aborted) return null;
  const storedCity = readLastFromCity(maxLength);
  if (storedCity) {
    return { city: storedCity, source: "storage" };
  }

  // 3. 调 BFF 按网络位置猜测
  if (signal?.aborted) return null;
  try {
    const res = await guessCity({ signal });
    if (signal?.aborted) return null;
    if (res?.ok) {
      const guessedCity = normalizeCityValue(res.city, maxLength);
      if (guessedCity) {
        return { city: guessedCity, source: "geo" };
      }
    }
  } catch {
    // 静默不预填、不报错
  }

  return null;
}

export interface UseFromCityPrefillOptions {
  value: string;
  onApplyPrefill: (city: string, source: FromCityPrefillSource) => void;
  authStatus: AuthStatus;
  bootstrapped: boolean;
  userId: string | null;
  enabled?: boolean;
  maxLength?: number;
  initialTouched?: boolean;
  initialSource?: FromCityPrefillSource | null;
}

export interface UseFromCityPrefillResult {
  prefillSource: FromCityPrefillSource | null;
  showGeoHint: boolean;
  markUserEdited: () => void;
  resetPrefillState: () => void;
}

/**
 * 统一的出发城市预填 Hook：
 * - 仅在输入框为空且用户尚未手动编辑/清空时执行预填；
 * - 任意手动输入或清空后锁定，不再被晚到响应覆盖，同时隐藏 IP 猜测提示；
 * - 账号切换或重置时可调用 resetPrefillState 重新允许预填。
 */
export function useFromCityPrefill({
  value,
  onApplyPrefill,
  authStatus,
  bootstrapped,
  userId,
  enabled = true,
  maxLength = DEFAULT_FROM_CITY_MAX_LENGTH,
  initialTouched = false,
  initialSource = null,
}: UseFromCityPrefillOptions): UseFromCityPrefillResult {
  const [prefillSource, setPrefillSource] =
    useState<FromCityPrefillSource | null>(initialSource);
  const userEditedRef = useRef<boolean>(initialTouched);
  const attemptedKeyRef = useRef<string | null>(null);
  const valueRef = useRef(value);
  valueRef.current = value;
  const onApplyPrefillRef = useRef(onApplyPrefill);
  onApplyPrefillRef.current = onApplyPrefill;

  const isEmpty = value.trim().length === 0;

  const markUserEdited = useCallback(() => {
    userEditedRef.current = true;
    setPrefillSource(null);
  }, []);

  const resetPrefillState = useCallback(() => {
    userEditedRef.current = false;
    attemptedKeyRef.current = null;
    setPrefillSource(null);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    if (authStatus === "unknown" && !bootstrapped) return;
    if (userEditedRef.current) return;
    if (!isEmpty) return;

    const attemptKey = `${authStatus}:${userId ?? ""}`;
    if (attemptedKeyRef.current === attemptKey) return;

    let cancelled = false;
    const controller = new AbortController();

    void resolveFromCityPrefill({
      isAuthenticated: authStatus === "authenticated",
      signal: controller.signal,
      maxLength,
    }).then((result) => {
      if (cancelled || controller.signal.aborted) return;
      attemptedKeyRef.current = attemptKey;
      if (userEditedRef.current) return;
      if (valueRef.current.trim().length > 0) return;
      if (!result) return;
      setPrefillSource(result.source);
      onApplyPrefillRef.current(result.city, result.source);
    });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [authStatus, bootstrapped, userId, enabled, isEmpty, maxLength]);

  return {
    prefillSource,
    showGeoHint: prefillSource === "geo" && !isEmpty,
    markUserEdited,
    resetPrefillState,
  };
}
