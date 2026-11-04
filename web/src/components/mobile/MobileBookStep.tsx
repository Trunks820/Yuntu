import React, { useState } from "react";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { MOBILE_AVOID_OPTIONS, MOBILE_SCENES } from "@/types/mobile";
import type { MobileStep, MobileSubmission } from "@/types/mobile";
import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";
import { CITY_PLACEHOLDER, cityPhotoSources } from "@/utils/cityPhotoSources";
import { MobileAvoidModal } from "./MobileAvoidModal";
import { Icon, Lines } from "./mobileUi";
import { findCachedDestination, formatMonthDay, hasSceneCategory, rhythmLabel, sceneTags } from "./mobileFormat";

interface MobileBookStepProps {
  onEditStep: (step: MobileStep) => void;
  onSubmit: () => void;
  isSubmitting?: boolean;
  pendingSubmission?: MobileSubmission | null;
  onRetrySame?: () => void;
  /** 回想去清单改这次带上的地点（只有「随手收集灵感」入口才有） */
  onEditPlaces?: () => void;
}

function coverTitle(sceneIds: string[]): string {
  if (hasSceneCategory(sceneIds, "food")) return "把日子，\n过得有滋有味";
  if (hasSceneCategory(sceneIds, "nature")) return "留几天，\n给山水和自己";
  if (hasSceneCategory(sceneIds, "night")) return "等灯火，\n也等一点惊喜";
  return "走慢一点，\n遇见这座城";
}

export const MobileBookStep: React.FC<MobileBookStepProps> = ({
  onEditStep,
  onSubmit,
  isSubmitting = false,
  pendingSubmission = null,
  onRetrySame,
  onEditPlaces,
}) => {
  const draft = useMobileTripStore((s) => s.draft);
  const [avoidModalOpen, setAvoidModalOpen] = useState(false);
  const isInspiration = draft.entry === "inspiration";

  const days = draft.days || 3;
  const city = draft.cityName || "心动目的地";
  const shortDate =
    draft.dateMode === "exact" && draft.startDate ? formatMonthDay(draft.startDate) : "日期未定";
  const partyLabel = `${draft.adults} 位成人${draft.children ? ` · ${draft.children} 位儿童` : ""}`;
  const tags = sceneTags(draft.sceneIds);
  const avoidTags = draft.avoidIds
    .map((id) => MOBILE_AVOID_OPTIONS.find((a) => a.id === id)?.label)
    .filter((t): t is string => Boolean(t));

  // 封面：优先用真实目录的城市图，没有时用第一张心动画面的概念图
  const cachedCity = findCachedDestination(draft.cityId, draft.cityName);
  const citySource = draft.cityName
    ? cityPhotoSources(draft.cityName, cachedCity ?? undefined)[0]
    : CITY_PLACEHOLDER;
  const cityCover = citySource === CITY_PLACEHOLDER ? null : citySource;
  const firstScene = MOBILE_SCENES.find((s) => s.id === draft.sceneIds[0]);
  const coverImage = cityCover || CONCEPT_ASSETS[firstScene?.image ?? "lake"];

  const manifest: string[] = [];
  if (tags.length) manifest.push(`已记录你的心动：${tags.join("、")}`);
  if (draft.rhythm) manifest.push(`已记录节奏偏好：${rhythmLabel(draft.rhythm)}`);
  if (avoidTags.length) manifest.push(`已记录叮嘱：${avoidTags.join("、")}`);

  const editTag = (
    label: string,
    icon: Parameters<typeof Icon>[0]["name"],
    text: string,
    onClick: () => void,
  ) => (
    <button type="button" aria-label={label} onClick={onClick}>
      <Icon name={icon} />
      {text}
    </button>
  );

  return (
    <>
      <div className="scroll">
        <div className="screen-inner page-pad">
          <div className="eyebrow">YOUR FEELINGS, TAKING SHAPE</div>
          <h2 className="page-title">
            刚刚那些心动，
            <br />
            已经<em>有了你的模样</em>。
          </h2>
          <p className="sub">这不是最终攻略，是刚刚一起留下的旅行草稿。</p>

          <div className="book-stage">
            <article className="travel-book">
              <span className="book-bookmark">{String(days).padStart(2, "0")}</span>
              <div className="book-kicker">YUNTU · YOUR PERSONAL TRAVEL BOOK</div>
              <div className="cover-image">
                <i className="tape" />
                <img src={coverImage} alt="旅行册封面配图" draggable={false} />
              </div>
              <h3 className="book-title">
                {city}。
                <br />
                <Lines text={coverTitle(draft.sceneIds)} />
              </h3>
              <div className="cover-bottom">
                <span>
                  {days} 天 · {shortDate}
                  <br />
                  写给{draft.companion === "solo" ? "自己的几天" : "一起出发的我们"}
                </span>
                <span style={{ fontFamily: "var(--serif)", fontSize: 17 }}>云途</span>
              </div>
            </article>
          </div>

          <div className="book-edit-tags">
            {editTag("修改目的地", "pin", city, () => onEditStep("profile"))}
            {editTag("修改天数与日期", "calendar", `${days} 天 · ${shortDate}`, () =>
              onEditStep("departure"),
            )}
            {editTag("修改同行情况", "people", partyLabel, () => onEditStep("companions"))}
            {isInspiration
              ? editTag("修改节奏", "clock", rhythmLabel(draft.rhythm), () =>
                  onEditStep("departure"),
                )
              : (
                  <>
                    {editTag("修改心动画面", "heart", tags.length ? tags.join(" · ") : "补一点喜好", () =>
                      onEditStep("moments"),
                    )}
                    {editTag("修改节奏", "clock", rhythmLabel(draft.rhythm), () => onEditStep("rhythm"))}
                  </>
                )}
            {editTag(
              "修改叮嘱与避好",
              "pen",
              avoidTags.length ? avoidTags.join(" · ") : "还有什么想叮嘱的？",
              () => setAvoidModalOpen(true),
            )}
          </div>
          <div className="book-live">
            点任意一张小标签，就能回去修改。
            <br />
            其他心动，都替你留着。
          </div>

          {draft.mustInclude.length > 0 && (
            <div className="inspiration-summary">
              {onEditPlaces ? (
                <button type="button" className="text-btn" onClick={onEditPlaces}>
                  这次想去的 {draft.mustInclude.length} 个地方 <Icon name="pen" />
                </button>
              ) : (
                <strong>这次想去的 {draft.mustInclude.length} 个地方</strong>
              )}
              <p>{draft.mustInclude.map((p) => p.name).join(" · ")}</p>
              <small>作为候选，是否能安排需再核验。</small>
            </div>
          )}

          <div className="manifest">
            {manifest.length ? (
              manifest.map((row) => (
                <div key={row} className="manifest-row">
                  <Icon name="spark" />
                  <span>{row}</span>
                </div>
              ))
            ) : (
              <div className="manifest-row">
                <Icon name="book" />
                <span>先按你选的目的地和天数，翻开这本旅行册。</span>
              </div>
            )}
            {draft.rawNotes && (
              <div className="manifest-row">
                <Icon name="pen" />
                <span>你留的话：{draft.rawNotes}</span>
              </div>
            )}
            <small>
              以上是你留下的偏好，提交后由云途生成真实行程；草稿本身未核验路线、营业时间与费用。
            </small>
          </div>

          {pendingSubmission?.status === "pending_confirmation" && (
            <div className="paper-note state-note warn pending-note" role="alert">
              <strong>上一笔提交结果待确认</strong>
              <br />
              网络在确认过程中断开或超时。为了防止重复扣除额度，原请求已被保护，可以直接以同一请求重试确认结果。
              <button
                type="button"
                className="primary center"
                onClick={onRetrySame}
                disabled={isSubmitting}
              >
                {isSubmitting ? "正在确认中…" : "以同一请求重试"}
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="footer-action">
        <button type="button" className="primary" onClick={onSubmit} disabled={isSubmitting}>
          <span>{isSubmitting ? "正在为你整理行程…" : "把这些心动，排成旅程"}</span>
          <Icon name="arrow" />
        </button>
      </div>

      {avoidModalOpen && <MobileAvoidModal onClose={() => setAvoidModalOpen(false)} />}
    </>
  );
};


