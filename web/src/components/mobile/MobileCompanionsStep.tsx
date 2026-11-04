import React, { useState } from "react";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import type { MobileCompanionRelation } from "@/types/mobile";
import { COMPANION_BOUNDARIES } from "@/types/mobile";
import { Icon } from "./mobileUi";
import type { MobileIconName } from "./mobileUi";

interface MobileCompanionsStepProps {
  onNext: () => void;
}

const OPTIONS: {
  id: MobileCompanionRelation;
  title: string;
  sub: string;
  icon: MobileIconName;
}[] = [
  { id: "solo", title: "一个人，自由出走", sub: "这几天，只跟着自己的心情", icon: "person" },
  { id: "partner", title: "和喜欢的人", sub: "一起收集新的记忆", icon: "heart" },
  { id: "friends", title: "和朋友一起", sub: "好玩的事，想一起做", icon: "people" },
  { id: "parents", title: "陪父母走走", sub: "把相处的时间留长一点", icon: "people" },
  { id: "kids", title: "带孩子看看", sub: "把世界翻开一小页", icon: "family" },
];

const PEOPLE_COPY: Record<MobileCompanionRelation, [string, string]> = {
  solo: ["这几天，只跟着自己的心情。", "把自己放进旅行册"],
  partner: ["把沿途的心动，变成两个人的回忆。", "把两个人放进旅行册"],
  friends: ["有些风景，一起看才更有意思。", "把我们放进旅行册"],
  parents: ["这一次，把相处的时间留长一点。", "把一家人放进旅行册"],
  kids: ["陪孩子看看，也重新发现世界。", "把一家人放进旅行册"],
};

const PALETTE = ["#37715d", "#b78269", "#557e99", "#c3a04b", "#91769d", "#8d995f"];
const COLOR_OFFSET: Record<MobileCompanionRelation, number> = {
  solo: 0,
  partner: 0,
  friends: 2,
  parents: 4,
  kids: 0,
};

const Avatar: React.FC<{ coat: string; child?: boolean }> = ({ coat, child }) => (
  <span
    className={`avatar${child ? " child" : ""}`}
    style={{ "--coat": coat } as React.CSSProperties}
  >
    <i className="head" />
    <i className="body" />
  </span>
);

export const MobileCompanionsStep: React.FC<MobileCompanionsStepProps> = ({
  onNext,
}) => {
  const draft = useMobileTripStore((s) => s.draft);
  const setCompanion = useMobileTripStore((s) => s.setCompanion);
  const setPeopleCounts = useMobileTripStore((s) => s.setPeopleCounts);
  const [expanded, setExpanded] = useState(false);

  const { companion, adults, children } = draft;
  const bounds = COMPANION_BOUNDARIES[companion];
  const total = adults + children;
  const adjustable = companion !== "solo";

  let [description, cta] = PEOPLE_COPY[companion];
  if (companion === "partner" && children) {
    description = "把沿途的心动，变成一家人的回忆。";
    cta = "把一家人放进旅行册";
  }
  if (draft.editReturn === "review") cta = "把同行人放回旅行册";

  const limitNote =
    total >= 6
      ? "这版先支持最多 6 人同行。"
      : children === 3
        ? "儿童最多 3 位，总人数不超过 6 位。"
        : companion === "partner"
          ? "两位成人，最多可带 3 位儿童。"
          : companion === "friends"
            ? "朋友同行，最多 6 位成人。"
            : "总人数最多 6 位，儿童最多 3 位。";

  const color = (i: number) => PALETTE[(i + COLOR_OFFSET[companion]) % PALETTE.length];
  const adultFigures = Array.from({ length: adults }, (_, i) => (
    <Avatar key={`a${i}`} coat={color(i)} />
  ));
  const childFigures = Array.from({ length: children }, (_, i) => (
    <Avatar key={`c${i}`} coat={color(adults + i)} child />
  ));
  const middle = Math.ceil(adults / 2);

  const handleSelect = (relation: MobileCompanionRelation) => {
    setCompanion(relation);
    setExpanded(false);
  };

  return (
    <>
      <div className="scroll">
        <div className="screen-inner page-pad">
          <div className="eyebrow">03 / PEOPLE MAKE THE JOURNEY</div>
          <h2 className="page-title">
            这本旅行册，
            <br />
            想和谁<em>一起写</em>？
          </h2>
          <p className="sub" aria-live="polite">
            {description}
          </p>

          <div
            className={`companion-scene people-scene people-${companion}${total > 5 ? " people-crowded" : ""}`}
            role="img"
            aria-label={`${adults} 位成人，${children} 位儿童`}
          >
            <div className="people-row">
              {adultFigures.slice(0, middle)}
              {childFigures}
              {adultFigures.slice(middle)}
            </div>
          </div>

          <div className="companion-grid" role="radiogroup" aria-label="同行关系">
            {OPTIONS.map((o) => {
              const selected = companion === o.id;
              return (
                <button
                  key={o.id}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  className={`companion-card${o.id === "solo" ? " wide" : ""}${selected ? " selected" : ""}`}
                  onClick={() => handleSelect(o.id)}
                >
                  <Icon name={o.icon} />
                  <span>
                    <strong>{o.title}</strong>
                    <small>{o.sub}</small>
                  </span>
                </button>
              );
            })}
          </div>

          <div className="people-summary">
            <span aria-live="polite">
              成人 {adults} 位{bounds.maxChildren > 0 ? ` · 儿童 ${children} 位` : ""}
            </span>
            {adjustable && (
              <button
                type="button"
                className="text-btn"
                onClick={() => setExpanded(!expanded)}
                aria-expanded={expanded}
                aria-controls="people-count-controls"
              >
                {expanded ? "收好人数" : "调整人数"}
              </button>
            )}
          </div>

          {expanded && adjustable && (
            <>
              <div className="count-ticket" id="people-count-controls">
                {bounds.minAdults !== bounds.maxAdults ? (
                  <div className="count-group">
                    <span className="label">成人</span>
                    <button
                      type="button"
                      disabled={adults <= bounds.minAdults}
                      onClick={() => setPeopleCounts(adults - 1, children)}
                      aria-label="减少成人"
                    >
                      −
                    </button>
                    <b>{adults}</b>
                    <button
                      type="button"
                      disabled={adults >= bounds.maxAdults || total >= bounds.maxTotal}
                      onClick={() => setPeopleCounts(adults + 1, children)}
                      aria-label="增加成人"
                    >
                      +
                    </button>
                  </div>
                ) : (
                  <span className="people-fixed">成人 {adults} 位</span>
                )}
                {bounds.maxChildren > 0 && (
                  <div className="count-group">
                    <span className="label">儿童</span>
                    <button
                      type="button"
                      disabled={children <= bounds.minChildren}
                      onClick={() => setPeopleCounts(adults, children - 1)}
                      aria-label="减少儿童"
                    >
                      −
                    </button>
                    <b>{children}</b>
                    <button
                      type="button"
                      disabled={children >= bounds.maxChildren || total >= bounds.maxTotal}
                      onClick={() => setPeopleCounts(adults, children + 1)}
                      aria-label="增加儿童"
                    >
                      +
                    </button>
                  </div>
                )}
              </div>
              <p className="people-limit" aria-live="polite">
                {limitNote}
              </p>
            </>
          )}
        </div>
      </div>

      <div className="footer-action">
        <button type="button" className="primary" onClick={onNext}>
          <span>{cta}</span>
          <Icon name="arrow" />
        </button>
      </div>
    </>
  );
};


