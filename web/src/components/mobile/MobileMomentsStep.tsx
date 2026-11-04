import React, { useState } from "react";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { canRotateBatch, getActiveBatchScenes } from "@/utils/mobileDraft";
import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";
import { Icon, Lines } from "./mobileUi";
import { sceneTags } from "./mobileFormat";

interface MobileMomentsStepProps {
  onNext: () => void;
}

const LIMIT = 4;

export const MobileMomentsStep: React.FC<MobileMomentsStepProps> = ({
  onNext,
}) => {
  const draft = useMobileTripStore((s) => s.draft);
  const toggleScene = useMobileTripStore((s) => s.toggleScene);
  const nextBatch = useMobileTripStore((s) => s.nextBatch);
  const [batchMessage, setBatchMessage] = useState("");

  const activeScenes = getActiveBatchScenes(draft.momentBatch, draft.sceneIds);
  const canRotate = canRotateBatch(draft.sceneIds);
  const count = draft.sceneIds.length;
  const tags = sceneTags(draft.sceneIds);

  const handleToggle = (sceneId: string) => {
    setBatchMessage("");
    toggleScene(sceneId);
  };

  const handleRefresh = () => {
    nextBatch();
    setBatchMessage(
      count ? "换好了，你选中的心动还在。" : "换好几个画面，再看看有没有喜欢的。",
    );
  };

  return (
    <>
      <div className="scroll">
        <div className="screen-inner page-pad">
          <div className="eyebrow">01 / COLLECT YOUR MOMENTS</div>
          <h2 className="page-title">
            先挑几个，
            <br />
            让你<em>想出发</em>的画面。
          </h2>
          <p className="sub">选 1–{LIMIT} 个就好，跟着此刻的感觉。</p>

          <div className="moment-grid" role="group" aria-label="心动场景列表">
            {activeScenes.map((scene) => {
              const selected = draft.sceneIds.includes(scene.id);
              return (
                <button
                  key={scene.id}
                  type="button"
                  className={`moment${selected ? " selected" : ""}`}
                  onClick={() => handleToggle(scene.id)}
                  aria-pressed={selected}
                  aria-label={scene.short}
                >
                  <img
                    src={CONCEPT_ASSETS[scene.image]}
                    alt="旅行场景概念配图"
                    draggable={false}
                  />
                  <span className="checkmark">
                    <Icon name="check" />
                  </span>
                  <span className="moment-copy">
                    <small>{scene.en}</small>
                    <strong>
                      <Lines text={scene.title} />
                    </strong>
                  </span>
                </button>
              );
            })}
          </div>

          <div style={{ display: "flex", justifyContent: "center", paddingTop: 12 }}>
            <button
              type="button"
              className="secondary"
              onClick={handleRefresh}
              disabled={!canRotate}
              aria-label="换几个画面"
            >
              <Icon name="undo" /> 还没心动？换几个画面
            </button>
          </div>

          <div className="selection-echo" role="status" aria-live="polite">
            {batchMessage ? (
              batchMessage
            ) : count ? (
              <>
                你留下了 <b>{tags.join(" · ")}</b>。
              </>
            ) : (
              "不用选“正确答案”，只选你真的心动的。"
            )}
            <br />
            <span style={{ fontSize: 12 }}>
              {count === LIMIT
                ? "四个心动已收好。取消一个，就能继续换画面。"
                : count
                  ? "已选的画面会留下，只换还没选的。"
                  : "换一批不影响进度，遇到喜欢的就能继续。"}
            </span>
          </div>
        </div>
      </div>

      <div className="footer-action">
        <button
          type="button"
          className="primary"
          onClick={onNext}
          disabled={count === 0}
        >
          <span>
            {draft.editReturn === "review" ? "把这些心动放回旅行册" : "把这几个心动留下"}
          </span>
          <Icon name="arrow" />
        </button>
        <div className="footer-note">
          {count} / {LIMIT} 个画面
        </div>
      </div>
    </>
  );
};


