import React from "react";
import type { TripSubmitRequest } from "@/types/tripRequest";
import { CITY_PLACEHOLDER, cityPhotoSources } from "@/utils/cityPhotoSources";
import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";
import { MOBILE_SCENES, SCENE_CATEGORY_INTEREST_MAP } from "@/types/mobile";
import { MobileTopBar } from "@/components/mobile/MobileTopBar";
import { Icon } from "@/components/mobile/mobileUi";
import "@/styles/mobile.css";

/** 原型成册四步文案，对应服务端四个生成阶段 */
const GEN_PHASES: [string, string][] = [
  ["把你留下的画面，放进封面", "旅行的第一笔，不是目的地，而是你的心动。"],
  ["给每一天，留好不同的篇幅", "不是把时间塞满，而是把喜欢的事放进去。"],
  ["把同行人和取舍，写进页边", "你说不想要的，也和喜欢的一样重要。"],
  ["装订好，这本只属于这次的旅行", "下一页，就能看到选择如何改变行程。"],
];

const INTEREST_LABELS: Record<string, string> = Object.fromEntries(
  MOBILE_SCENES.map((s) => [SCENE_CATEGORY_INTEREST_MAP[s.category], s.tag]),
);

export interface MobilePlanningViewProps {
  destination: string;
  formData: TripSubmitRequest | null;
  stageIndex: number;
  stageLabel: string | null;
  quoteText: string;
  failed: boolean;
  /** 状态暂时无法确认（含超时） */
  unknownState: boolean;
  errorMessage: string | null;
  networkUnstable: boolean;
  pauseReason: string | null;
  retrying: boolean;
  /** 已生成完成时的自动跳转倒计时；未完成为 null */
  readyCountdown: number | null;
  onOpenResult: () => void;
  onRetrySame: () => void;
  onReplan: () => void;
  onRecheck: () => void;
  onHome: () => void;
}

/** 手机端「旅行册装订中」：只负责呈现，轮询与重试逻辑都在 PlanningPage */
export const MobilePlanningView: React.FC<MobilePlanningViewProps> = ({
  destination,
  formData,
  stageIndex,
  stageLabel,
  quoteText,
  failed,
  unknownState,
  errorMessage,
  networkUnstable,
  pauseReason,
  retrying,
  readyCountdown,
  onOpenResult,
  onRetrySame,
  onReplan,
  onRecheck,
  onHome,
}) => {
  const ready = readyCountdown !== null;
  const city = destination === "当前目的地" ? formData?.to_city || "旅行" : destination;
  const days = formData?.days;
  const people = formData?.people_count;
  const prefs = formData?.preferences ?? [];
  const slow = prefs.includes("慢一点");
  const firstTag = prefs.map((p) => INTEREST_LABELS[p]).find(Boolean) || "新的期待";
  const thirdTag = formData?.avoid?.[0] || (slow ? "慢一点" : prefs.includes("充实一点") ? "充实一点" : "跟着心动");
  const photo = cityPhotoSources(city)[0];
  const cover = photo === CITY_PLACEHOLDER ? CONCEPT_ASSETS.lake : photo;
  const phase = GEN_PHASES[Math.min(3, Math.max(0, stageIndex))];

  const eyebrow = failed
    ? "SOMETHING GOT IN THE WAY"
    : ready
      ? "YOUR TRAVEL BOOK IS READY"
      : "YOUR TRIP IS COMING TOGETHER";
  const title = failed
    ? "这次，没能排成\n可以出发的日子。"
    : unknownState
      ? "旅行册还在路上，\n状态暂时没同步到。"
      : ready
        ? "装订好了，\n翻开看看这几天。"
        : "正在把心动，\n排成可以出发的日子。";

  return (
    <div className="ym">
      <div className="ym-device">
        <section className="screen generating" aria-label="旅行册装订中">
          <MobileTopBar title={failed ? "这次没能成册" : "旅行册装订中"} onBack={onHome} />
          <div className="scroll">
            <div className="screen-inner page-pad">
              <div className="eyebrow" style={{ justifyContent: "center", marginTop: 12 }}>
                {eyebrow}
              </div>
              <h2 className="page-title">
                {title.split("\n").map((line, i) => (
                  <React.Fragment key={i}>
                    {i > 0 && <br />}
                    {line}
                  </React.Fragment>
                ))}
              </h2>

              <div className="assembly">
                <div className="assembly-page">
                  <small>YUNTU / {days ? `${String(days).padStart(2, "0")} DAYS` : "TRAVEL BOOK"}</small>
                  <img src={cover} alt={`${city} · 旅行册封面`} draggable={false} />
                  <h3>
                    {city}
                    <br />
                    {slow ? "慢慢来就好" : "遇见新日常"}
                  </h3>
                </div>
                <span className="flying-sticker one">{firstTag}</span>
                {days ? (
                  <span className="flying-sticker two">
                    {days} 天{people ? ` · ${people} 人同行` : ""}
                  </span>
                ) : null}
                <span className="flying-sticker three">{thirdTag}</span>
              </div>

              <div className="assembly-step" aria-live="polite">
                {failed ? "这一本，先停在这里" : ready ? "旅行册已经装订好" : phase[0]}
                <small>
                  {failed || unknownState
                    ? errorMessage || "旅行选择都还留着，可以再试一次。"
                    : ready
                      ? "马上为你翻开第一页。"
                      : stageLabel
                        ? `${stageLabel} · ${quoteText}`
                        : phase[1]}
                </small>
              </div>
              <div className="assembly-progress" aria-label={`第 ${stageIndex + 1} / 4 步`}>
                {GEN_PHASES.map((_, i) => (
                  <span key={i} className={ready || (!failed && i <= stageIndex) ? "done" : ""} />
                ))}
              </div>

              {networkUnstable && !failed && !unknownState && !pauseReason && (
                <p className="paper-note state-note" style={{ marginTop: 20 }} role="status">
                  网络有点弱，正在继续同步进度…
                </p>
              )}
              {pauseReason && !failed && !unknownState && (
                <p className="paper-note state-note warn" style={{ marginTop: 20 }} role="status">
                  {pauseReason === "consecutive_errors"
                    ? "网络连续 3 次没连上，先暂停自动更新。"
                    : "已经查询了 6 分钟，先暂停自动更新。"}
                  <br />
                  服务器上的生成不受影响，重新查询不会重复提交，也不会多扣额度。
                </p>
              )}

              {!failed && !unknownState && !ready && (
                <p className="demo-note">
                  已开启后台托管，可以随时离开
                  <br />
                  完成后会留在「我的行程」 · 预计 30–45 秒
                </p>
              )}
            </div>
          </div>

          <div className="footer-action">
            {ready ? (
              <button type="button" className="primary" onClick={onOpenResult}>
                <span>翻开旅行册 · {readyCountdown}s</span>
                <Icon name="arrow" />
              </button>
            ) : failed ? (
              <>
                <button type="button" className="primary" onClick={onRetrySame} disabled={retrying}>
                  <span>{retrying ? "正在重新提交…" : "再试一次"}</span>
                  <Icon name="arrow" />
                </button>
                <button type="button" className="secondary" onClick={onReplan}>
                  换个想法，重新规划
                </button>
              </>
            ) : unknownState || pauseReason ? (
              <>
                <button type="button" className="primary" onClick={onRecheck} disabled={retrying}>
                  <span>{retrying ? "正在查询…" : "重新查询状态"}</span>
                  <Icon name="arrow" />
                </button>
                <button type="button" className="secondary" onClick={onReplan}>
                  重新规划
                </button>
              </>
            ) : (
              <button type="button" className="secondary" onClick={onHome}>
                先收起来，回首页看看
              </button>
            )}
          </div>
        </section>
      </div>
    </div>
  );
};


