import { useCallback } from "react";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { MOBILE_FROM_CITY_MAX } from "@/types/mobile";
import { saveMobileDraft } from "@/utils/mobileDraft";
import {
  useFromCityPrefill,
  type FromCityPrefillSource,
} from "@/utils/fromCityPrefill";

export interface UseMobileFromCityResult {
  fromCity: string;
  showGeoHint: boolean;
  setFromCity: (next: string) => void;
}

/**
 * 手机出发票上的出发城市（开源免登录版）：
 * - 空着且没手动改过时按「本机历史上一单 → 本机上次出发地 → 网络位置」预填，预填不算一次修改（不动 revision）；
 * - 手动输入或清空后锁定，不再自动填。
 */
export function useMobileFromCity(): UseMobileFromCityResult {
  const fromCity = useMobileTripStore((s) => s.draft.fromCity ?? "");
  const fromCityTouched = useMobileTripStore((s) =>
    Boolean(s.draft.fromCityTouched),
  );
  const fromCitySource = useMobileTripStore(
    (s) => s.draft.fromCitySource ?? null,
  );

  const handleApplyPrefill = useCallback(
    (city: string, source: FromCityPrefillSource) => {
      useMobileTripStore.setState((state) => {
        if (
          state.draft.fromCityTouched ||
          (state.draft.fromCity || "").trim().length > 0
        ) {
          return state;
        }
        const next = {
          ...state.draft,
          fromCity: city.slice(0, MOBILE_FROM_CITY_MAX),
          fromCitySource: source,
          updatedAt: Date.now(),
        };
        saveMobileDraft(next);
        return { draft: next };
      });
    },
    [],
  );

  const { showGeoHint, markUserEdited } = useFromCityPrefill({
    value: fromCity,
    onApplyPrefill: handleApplyPrefill,
    authStatus: "authenticated",
    bootstrapped: true,
    userId: "guest",
    maxLength: MOBILE_FROM_CITY_MAX,
    initialTouched: fromCityTouched,
    initialSource: fromCitySource,
  });

  const setFromCity = useCallback(
    (nextRaw: string) => {
      markUserEdited();
      const value = nextRaw.slice(0, MOBILE_FROM_CITY_MAX);
      useMobileTripStore.setState((state) => {
        const next = {
          ...state.draft,
          revision: state.draft.revision + 1,
          fromCity: value,
          fromCityTouched: true,
          fromCitySource: null,
          updatedAt: Date.now(),
        };
        saveMobileDraft(next);
        return { draft: next };
      });
    },
    [markUserEdited],
  );

  return { fromCity, showGeoHint, setFromCity };
}
