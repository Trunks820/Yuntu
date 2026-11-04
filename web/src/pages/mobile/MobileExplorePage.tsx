import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { buildMobileTripRequest } from "@/utils/mobileDraft";
import { checkPoiSelection, type PoiSelectionResponse } from "@/api/poi";
import { submitTrip } from "@/services/api";
import { ApiRequestError } from "@/services/errors";
import {
  getMobileSubmission,
  saveMobileSubmission,
} from "@/utils/mobileSubmission";
import type { MobileStep } from "@/types/mobile";
import { MobileTopBar } from "@/components/mobile/MobileTopBar";
import { MobileStepProgress } from "@/components/mobile/MobileStepProgress";
import { MobileMomentsStep } from "@/components/mobile/MobileMomentsStep";
import { MobileRhythmStep } from "@/components/mobile/MobileRhythmStep";
import { MobileDepartureStep } from "@/components/mobile/MobileDepartureStep";
import { MobileCompanionsStep } from "@/components/mobile/MobileCompanionsStep";
import { MobileBookStep } from "@/components/mobile/MobileBookStep";
import { Icon } from "@/components/mobile/mobileUi";
import "@/styles/mobile.css";

const GUEST_OWNER = "guest";

export const MobileExplorePage: React.FC = () => {
  const navigate = useNavigate();
  const draft = useMobileTripStore((s) => s.draft);
  const setStep = useMobileTripStore((s) => s.setStep);
  const setEditReturn = useMobileTripStore((s) => s.setEditReturn);
  const cancelEdit = useMobileTripStore((s) => s.cancelEdit);
  const resetDraft = useMobileTripStore((s) => s.resetDraft);
  const setMustInclude = useMobileTripStore((s) => s.setMustInclude);

  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);


  const showToast = (msg: string) => {
    setToastMessage(msg);
    setTimeout(() => {
      setToastMessage(null);
    }, 2800);
  };

  // 标题映射
  const getStepTitle = (step: MobileStep): string => {
    switch (step) {
      case "moments":
        return "收集心动";
      case "rhythm":
        return "找到旅行的节奏";
      case "profile":
        return "你的旅行底色";
      case "departure":
        return "给旅程一点时间";
      case "companions":
        return "带上想同行的人";
      case "review":
        return "你的旅行，正在成形";
      default:
        return "云途旅行";
    }
  };

  // 下一步流转逻辑（严格支持 editReturn 返回汇总页）
  const handleNextFrom = (current: MobileStep) => {
    if (draft.editReturn === "review") {
      setEditReturn(null);
      setStep("review");
      return;
    }

    switch (current) {
      case "moments":
        setStep("rhythm");
        break;
      case "rhythm":
        setStep("departure");
        break;
      case "profile":
        setStep("departure");
        break;
      case "departure":
        setStep("companions");
        break;
      case "companions":
        setStep("review");
        break;
      default:
        break;
    }
  };

  // 返回上一步流转逻辑
  const handleBack = () => {
    if (draft.editReturn === "review") {
      cancelEdit();
      return;
    }

    switch (draft.step) {
      case "moments":
        navigate("/");
        break;
      case "rhythm":
        setStep("moments");
        break;
      case "profile":
        setStep("rhythm");
        break;
      case "departure":
        // 「随手收集灵感」没有前面几步，退回想去清单
        if (draft.entry === "inspiration") navigate("/explore/inspiration", { state: { openCity: draft.cityName } });
        else setStep("rhythm");
        break;
      case "companions":
        setStep("departure");
        break;
      case "review":
        setStep("companions");
        break;
      default:
        navigate("/");
        break;
    }
  };

  // 从汇总页直接编辑特定步骤
  const handleEditStep = (targetStep: MobileStep) => {
    setEditReturn("review");
    setStep(targetStep);
  };

  const submitGuestTrip = async () => {
    if (isSubmitting) return;
    if (!draft.cityConfirmed || !draft.cityName) {
      showToast("请先选择心动的目的地");
      setEditReturn("review");
      setStep("departure");
      return;
    }
    if (!draft.days || draft.days < 1 || draft.days > 7) {
      showToast("请先确认 1–7 天行程天数");
      setEditReturn("review");
      setStep("departure");
      return;
    }
    if (draft.mustInclude.length > 0) {
      let items: PoiSelectionResponse["items"];
      try {
        const res = await checkPoiSelection(
          draft.cityName,
          draft.mustInclude.map((p) => p.placeId),
        );
        items = res.items;
      } catch {
        showToast("没能核对带上的地点，请稍后再试");
        return;
      }
      const changed = draft.mustInclude.some((p) => {
        const item = items.find((i) => i.place_id === p.placeId);
        return !item || item.status !== "available" || item.place.name !== p.name;
      });
      if (changed) {
        const available = draft.mustInclude.flatMap((p) => {
          const item = items.find((i) => i.place_id === p.placeId);
          return item && item.status === "available"
            ? [{ placeId: p.placeId, name: item.place.name }]
            : [];
        });
        setMustInclude(available);
        showToast("带上的地点有变化，已按最新目录更新，请确认后再提交");
        return;
      }
    }
    const request = buildMobileTripRequest(draft);
    setIsSubmitting(true);
    try {
      const submission = saveMobileSubmission(request, GUEST_OWNER, draft.revision);
      const result = await submitTrip(request, submission.request_id);
      resetDraft();
      navigate(`/planning/${result.job_id}`);
    } catch (err) {
      const message = err instanceof ApiRequestError ? err.message : "提交失败，请稍后再试";
      showToast(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSubmit = () => {
    void submitGuestTrip();
  };

  const handleRetrySame = () => {
    void submitGuestTrip();
  };

  const pendingSubmission = getMobileSubmission(GUEST_OWNER);

  const screenClass = draft.step === "review" ? "book" : draft.step;

  return (
    <div className="ym">
      <div className="ym-device">
        <section
          className={`screen ${screenClass}`}
          aria-label={getStepTitle(draft.step)}
        >
          <MobileTopBar
            title={getStepTitle(draft.step)}
            onBack={handleBack}
            onHome={() => navigate("/")}
          />
          <MobileStepProgress currentStep={draft.step} />

          {draft.editReturn === "review" && (
            <div className="inline-return edit-return">
              <span>
                <Icon name="pen" />
                正在修改旅行册，其他选择都替你留着
              </span>
              <button type="button" className="text-btn" onClick={cancelEdit}>
                取消修改
              </button>
            </div>
          )}

          {draft.step === "moments" && (
            <MobileMomentsStep onNext={() => handleNextFrom("moments")} />
          )}

          {draft.step === "rhythm" && (
            <MobileRhythmStep onNext={() => handleNextFrom("rhythm")} />
          )}

          {draft.step === "departure" && (
            <MobileDepartureStep onNext={() => handleNextFrom("departure")} />
          )}

          {draft.step === "companions" && (
            <MobileCompanionsStep onNext={() => handleNextFrom("companions")} />
          )}

          {draft.step === "review" && (
            <MobileBookStep
              onEditStep={handleEditStep}
              onSubmit={handleSubmit}
              isSubmitting={isSubmitting}
              pendingSubmission={pendingSubmission}
              onRetrySame={handleRetrySame}
              onEditPlaces={
                draft.entry === "inspiration"
                  ? () => navigate("/explore/inspiration", { state: { openCity: draft.cityName } })
                  : undefined
              }
            />
          )}
        </section>

        {toastMessage && (
          <div className="toast" role="status" aria-live="polite">
            {toastMessage}
          </div>
        )}
      </div>

    </div>
  );
};

export default MobileExplorePage;


