import React from "react";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import type { MobileRhythm } from "@/types/mobile";
import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";
import { Icon, Lines } from "./mobileUi";

interface MobileRhythmStepProps {
  onNext: () => void;
}

const OPTIONS: { key: MobileRhythm; title: string; sub: string; image: string; alt: string }[] = [
  {
    key: "slow",
    title: "留点空白，\n喜欢就多待一会儿",
    sub: "少安排几站，给沿途的偶遇留时间。",
    image: CONCEPT_ASSETS.lake,
    alt: "水边停留的悠闲画面",
  },
  {
    key: "full",
    title: "多走几处，\n让这一天丰富一点",
    sub: "愿意多走一些，多体验几种当地生活。",
    image: CONCEPT_ASSETS.alley,
    alt: "探索街巷的旅行画面",
  },
];

export const MobileRhythmStep: React.FC<MobileRhythmStepProps> = ({
  onNext,
}) => {
  const draft = useMobileTripStore((s) => s.draft);
  const setRhythm = useMobileTripStore((s) => s.setRhythm);

  return (
    <>
      <div className="scroll">
        <div className="screen-inner page-pad">
          <div className="eyebrow">01 / SET YOUR OWN PACE</div>
          <h2 className="page-title">
            如果此刻就在旅途中，
            <br />
            你想怎么<em>度过一天</em>？
          </h2>
          <p className="sub">选一个更心动的，只决定这次的行程松紧。</p>

          <div role="radiogroup" aria-label="旅行节奏选择">
            {OPTIONS.map((opt) => {
              const selected = draft.rhythm === opt.key;
              return (
                <button
                  key={opt.key}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  className={`rhythm-card${selected ? " selected" : ""}`}
                  onClick={() => setRhythm(opt.key)}
                >
                  <img src={opt.image} alt={opt.alt} draggable={false} />
                  <div className="rhythm-content">
                    <strong style={{ lineHeight: 1.45 }}>
                      <Lines text={opt.title} />
                    </strong>
                    <small style={{ fontSize: 12, lineHeight: 1.6, opacity: 1 }}>
                      {opt.sub}
                    </small>
                  </div>
                  <span className="checkmark">
                    <Icon name="check" />
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      <div className="footer-action">
        <button
          type="button"
          className="primary"
          onClick={onNext}
          disabled={!draft.rhythm}
        >
          <span>
            {draft.editReturn === "review" ? "把这个节奏放回旅行册" : "看看这次的旅行感觉"}
          </span>
          <Icon name="arrow" />
        </button>
      </div>
    </>
  );
};


