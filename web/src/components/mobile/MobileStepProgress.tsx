import React from "react";
import type { MobileStep } from "@/types/mobile";

interface MobileStepProgressProps {
  currentStep: MobileStep;
}

/** 原型四段进度：心动（场景/节奏/底色）→ 出发票 → 同行人 → 旅行册 */
const STEP_INDEX: Partial<Record<MobileStep, number>> = {
  moments: 0,
  rhythm: 0,
  profile: 0,
  departure: 1,
  companions: 2,
  review: 3,
};

export const MobileStepProgress: React.FC<MobileStepProgressProps> = ({
  currentStep,
}) => {
  const activeIndex = STEP_INDEX[currentStep] ?? 0;

  return (
    <div
      className="step-line"
      role="progressbar"
      aria-label="当前体验阶段"
      aria-valuenow={activeIndex + 1}
      aria-valuemin={1}
      aria-valuemax={4}
    >
      {[0, 1, 2, 3].map((idx) => (
        <span key={idx} className={idx <= activeIndex ? "active" : ""} />
      ))}
    </div>
  );
};


