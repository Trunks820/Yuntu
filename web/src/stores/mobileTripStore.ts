import { create } from "zustand";
import type {
  MobileAvoidId,
  MobileCompanionRelation,
  MobileMustIncludePlace,
  MobileRhythm,
  MobileStep,
  MobileTripDraft,
} from "@/types/mobile";
import { COMPANION_BOUNDARIES, MOBILE_MUST_INCLUDE_LIMIT } from "@/types/mobile";
import {
  clampCompanionCounts,
  clearMobileEditSnapshot,
  createInitialMobileDraft,
  handleAccountSwitchForMobileDraft,
  loadMobileDraft,
  loadMobileEditSnapshot,
  resumeMobileDraftStep,
  saveMobileDraft,
  saveMobileEditSnapshot,
} from "@/utils/mobileDraft";

interface MobileTripState {
  draft: MobileTripDraft;
  setStep: (step: MobileStep) => void;
  toggleScene: (sceneId: string) => void;
  nextBatch: () => void;
  setRhythm: (rhythm: MobileRhythm | null) => void;
  setCity: (
    cityId: string | null,
    cityName: string | null,
    confirmed?: boolean,
  ) => void;
  setDays: (days: number) => void;
  setDateMode: (mode: "undecided" | "exact") => void;
  setStartDate: (date: string | null) => void;
  setCompanion: (companion: MobileCompanionRelation) => void;
  setPeopleCounts: (adults: number, children: number) => void;
  toggleAvoid: (avoidId: MobileAvoidId) => void;
  setAvoidIds: (avoidIds: MobileAvoidId[]) => void;
  setRawNotes: (notes: string) => void;
  editSnapshot: MobileTripDraft | null;
  setEditReturn: (step: MobileStep | null) => void;
  cancelEdit: () => void;
  resumeStep: () => MobileStep;
  /** 「已经想好去哪」选定城市：替换心动画面与节奏，从挑画面开始 */
  startDirectTrip: (cityId: string, cityName: string) => void;
  /** 「随手收集灵感」带着这座城的想去地点出发：同样替换心动画面与节奏，从挑画面开始 */
  startInspirationTrip: (
    cityId: string | null,
    cityName: string,
    places: MobileMustIncludePlace[],
  ) => void;
  /** 提交前复核后，用目录的最新结果替换带上的地点 */
  setMustInclude: (places: MobileMustIncludePlace[]) => void;
  resetDraft: () => void;
  syncAuthUser: (userId: string | null) => void;
}

export const useMobileTripStore = create<MobileTripState>()((set, get) => {
  const initialDraft = loadMobileDraft() || createInitialMobileDraft();
  const initialSnapshot = loadMobileEditSnapshot();

  const updateDraft = (updater: (prev: MobileTripDraft) => MobileTripDraft) => {
    set((state) => {
      const next = updater(state.draft);
      const nextWithMeta: MobileTripDraft = {
        ...next,
        revision: next.revision + 1,
        updatedAt: Date.now(),
      };
      saveMobileDraft(nextWithMeta);
      return { draft: nextWithMeta };
    });
  };

  return {
    draft: initialDraft,

    setStep: (step) =>
      updateDraft((d) => ({
        ...d,
        step,
      })),

    toggleScene: (sceneId) =>
      updateDraft((d) => {
        const current = d.sceneIds;
        if (current.includes(sceneId)) {
          return { ...d, sceneIds: current.filter((id) => id !== sceneId) };
        }
        if (current.length >= 4) {
          return d; // 最多选择 4 张
        }
        return { ...d, sceneIds: [...current, sceneId] };
      }),

    nextBatch: () =>
      updateDraft((d) => {
        if (d.sceneIds.length >= 4) {
          return d; // 选满 4 张禁用换一批
        }
        return { ...d, momentBatch: (d.momentBatch + 1) % 3 };
      }),

    setRhythm: (rhythm) =>
      updateDraft((d) => ({
        ...d,
        rhythm,
      })),

    setCity: (cityId, cityName, confirmed = true) =>
      updateDraft((d) => ({
        ...d,
        cityId,
        cityName,
        cityConfirmed: Boolean(confirmed && cityName),
        // 带上的地点只属于原来那座城
        mustInclude: cityName === d.cityName ? d.mustInclude : [],
      })),

    setDays: (days) =>
      updateDraft((d) => ({
        ...d,
        days: Math.min(7, Math.max(1, days)),
      })),

    setDateMode: (dateMode) =>
      updateDraft((d) => ({
        ...d,
        dateMode,
      })),

    setStartDate: (startDate) =>
      updateDraft((d) => ({
        ...d,
        startDate,
      })),

    setCompanion: (companion) =>
      updateDraft((d) => {
        const bounds = COMPANION_BOUNDARIES[companion];
        return {
          ...d,
          companion,
          adults: bounds.defaultAdults,
          children: bounds.defaultChildren,
        };
      }),

    setPeopleCounts: (adults, children) =>
      updateDraft((d) => {
        const clamped = clampCompanionCounts(d.companion, adults, children);
        return {
          ...d,
          adults: clamped.adults,
          children: clamped.children,
        };
      }),

    toggleAvoid: (avoidId) =>
      updateDraft((d) => {
        const current = d.avoidIds;
        if (current.includes(avoidId)) {
          return {
            ...d,
            avoidIds: current.filter((id) => id !== avoidId),
          };
        }
        return {
          ...d,
          avoidIds: [...current, avoidId],
        };
      }),

    setAvoidIds: (avoidIds) =>
      updateDraft((d) => ({
        ...d,
        avoidIds,
      })),

    setRawNotes: (notes) =>
      updateDraft((d) => ({
        ...d,
        rawNotes: notes.slice(0, 300),
      })),

    editSnapshot: initialSnapshot,

    setEditReturn: (editReturn) => {
      set((state) => {
        const next: MobileTripDraft = {
          ...state.draft,
          editReturn,
          revision: state.draft.revision + 1,
          updatedAt: Date.now(),
        };
        saveMobileDraft(next);
        const snapshot = editReturn ? { ...state.draft } : null;
        if (snapshot) {
          saveMobileEditSnapshot(snapshot);
        } else {
          clearMobileEditSnapshot();
        }
        return {
          draft: next,
          editSnapshot: snapshot,
        };
      });
    },

    cancelEdit: () => {
      set((state) => {
        const snapshot = state.editSnapshot || loadMobileEditSnapshot();
        clearMobileEditSnapshot();
        if (snapshot) {
          const restored: MobileTripDraft = {
            ...snapshot,
            step: "review",
            editReturn: null,
            revision: state.draft.revision + 1,
            updatedAt: Date.now(),
          };
          saveMobileDraft(restored);
          return { draft: restored, editSnapshot: null };
        }
        const updated: MobileTripDraft = {
          ...state.draft,
          step: "review",
          editReturn: null,
          revision: state.draft.revision + 1,
          updatedAt: Date.now(),
        };
        saveMobileDraft(updated);
        return { draft: updated, editSnapshot: null };
      });
    },

    resumeStep: () => {
      const current = get().draft;
      const targetStep = resumeMobileDraftStep(current);
      get().setStep(targetStep);
      return targetStep;
    },

    startDirectTrip: (cityId, cityName) => {
      clearMobileEditSnapshot();
      set({ editSnapshot: null });
      updateDraft((d) => ({
        ...d,
        entry: "direct",
        step: "moments",
        sceneIds: [],
        seenSceneIds: [],
        momentBatch: 0,
        rhythm: null,
        cityId,
        cityName,
        cityConfirmed: true,
        mustInclude: [],
        editReturn: null,
      }));
    },

    startInspirationTrip: (cityId, cityName, places) => {
      clearMobileEditSnapshot();
      set({ editSnapshot: null });
      // 照原型：不挑画面，直接到出发；同一座城接着改时保留已选的节奏
      updateDraft((d) => ({
        ...d,
        entry: "inspiration",
        step: "departure",
        sceneIds: [],
        seenSceneIds: [],
        momentBatch: 0,
        rhythm: d.entry === "inspiration" && d.cityName === cityName ? d.rhythm : null,
        cityId,
        cityName,
        cityConfirmed: true,
        mustInclude: places.slice(0, MOBILE_MUST_INCLUDE_LIMIT),
        editReturn: null,
      }));
    },

    setMustInclude: (places) =>
      updateDraft((d) => ({
        ...d,
        mustInclude: places.slice(0, MOBILE_MUST_INCLUDE_LIMIT),
      })),

    resetDraft: () => {
      clearMobileEditSnapshot();
      const fresh = createInitialMobileDraft(get().draft.ownerUserId);
      saveMobileDraft(fresh);
      set({ draft: fresh, editSnapshot: null });
    },

    syncAuthUser: (userId) => {
      const current = get().draft;
      const updated = handleAccountSwitchForMobileDraft(current, userId);
      if (updated && updated !== current) {
        clearMobileEditSnapshot();
        saveMobileDraft(updated);
        set({ draft: updated, editSnapshot: null });
      }
    },
  };
});


