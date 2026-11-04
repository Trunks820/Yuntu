import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { CONCEPT_ASSETS, HOME_HERO_IMAGE } from "@/assets/mobile-concepts/conceptImages";
import { MobileTopBar } from "@/components/mobile/MobileTopBar";
import { Icon, MobileSheet } from "@/components/mobile/mobileUi";
import { MobileLiveCityPicker } from "@/components/mobile/MobileCitySheet";
import type { Destination } from "@/types/destination";
import { useTripTaskStore, type StoredTripTask } from "@/stores/tripTaskStore";
import { mobileHomeCardTarget, selectMobileHomeCard } from "@/utils/mobileHomeCard";
import "@/styles/mobile.css";

/** 原型 .resume 卡片在生成中 / 已成形时的文案；失败态原型没有，按同一语气补齐 */
const TASK_CARD_COPY = {
  generating: { title: "旅行册正在装订", hint: "看看进度" },
  ready: { title: "你的旅行册已经成形", hint: "翻开看看" },
  failed: { title: "这次没能成册", hint: "看看原因，再试一次" },
} as const;

/** 首页轮播：01 为原型原文；02、03 对应草稿流程里的「节奏」「同行」两步，配图为同组氛围图 */
const HERO_SLIDES = [
  {
    label: "01 / 心动",
    image: HOME_HERO_IMAGE,
    alt: "旅行场景概念配图",
    eyebrow: "A TRIP THAT FEELS LIKE YOU",
    title: ["下一段旅程，", "先找到心动。"],
    desc: ["不必想好所有答案。", "挑几个喜欢的画面，旅程就有了开头。"],
  },
  {
    label: "02 / 节奏",
    image: CONCEPT_ASSETS.coast,
    alt: "旅行节奏概念配图",
    eyebrow: "AT YOUR OWN PACE",
    title: ["走快走慢，", "都随你的步子。"],
    desc: ["想早起追日出，还是睡到自然醒？", "每天排多满，由你来定。"],
  },
  {
    label: "03 / 同行",
    image: CONCEPT_ASSETS.food,
    alt: "同行旅伴概念配图",
    eyebrow: "WHO YOU GO WITH",
    title: ["和谁同行，", "旅程就有了样子。"],
    desc: ["一个人自在，一家人热闹。", "说说这次和谁去，安排会更合你们。"],
  },
] as const;

const HERO_SLIDE_MS = 5000;

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

export const MobileHomePage: React.FC = () => {
  const navigate = useNavigate();
  const draft = useMobileTripStore((s) => s.draft);
  const setStep = useMobileTripStore((s) => s.setStep);
  const resumeStep = useMobileTripStore((s) => s.resumeStep);
  const startDirectTrip = useMobileTripStore((s) => s.startDirectTrip);
  const [directStage, setDirectStage] = useState<"confirm" | "cities" | null>(null);

  const [slideIndex, setSlideIndex] = useState(0);
  const touchStartX = useRef<number | null>(null);
  const slide = HERO_SLIDES[slideIndex];
  const goToSlide = (i: number) =>
    setSlideIndex((i + HERO_SLIDES.length) % HERO_SLIDES.length);

  // 每屏停留后自动切换；手动切换会重新计时；系统开启「减少动态效果」时不自动播放
  useEffect(() => {
    if (prefersReducedMotion()) return;
    const timer = window.setTimeout(
      () => setSlideIndex((i) => (i + 1) % HERO_SLIDES.length),
      HERO_SLIDE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [slideIndex]);

  const hasDraftProgress = Boolean(
    draft.sceneIds.length > 0 ||
      draft.cityName ||
      draft.cityConfirmed ||
      draft.rhythm,
  );

  const tasks = useTripTaskStore((s) => s.tasks);
  const acknowledgeTask = useTripTaskStore((s) => s.acknowledgeTask);
  const homeCard = selectMobileHomeCard(hasDraftProgress, tasks);

  const handleOpenTask = (task: StoredTripTask) => {
    if (task.status !== "pending" && task.status !== "timeout") {
      acknowledgeTask(task.jobId);
    }
    navigate(mobileHomeCardTarget(task));
  };

  const handleStartQuiz = () => {
    setStep("moments");
    navigate("/explore");
  };

  const handleResume = () => {
    resumeStep();
    navigate("/explore");
  };

  // 已有草稿进度时先确认；选定城市那一刻才替换心动画面与节奏，取消则草稿不变
  const handleDirect = () => setDirectStage(hasDraftProgress ? "confirm" : "cities");

  const handleDirectCity = (dest: Destination) => {
    startDirectTrip(dest.id, dest.name);
    setDirectStage(null);
    navigate("/explore");
  };

  return (
    <div className="ym">
      <div className="ym-device">
        <section className="screen home" aria-label="云途首页">
          <div className="scroll">
            <div
              className="home-hero"
              role="region"
              aria-roledescription="carousel"
              aria-label="云途旅行灵感"
              onTouchStart={(e) => {
                touchStartX.current = e.touches[0]?.clientX ?? null;
              }}
              onTouchEnd={(e) => {
                const start = touchStartX.current;
                touchStartX.current = null;
                const end = e.changedTouches[0]?.clientX;
                if (start === null || end === undefined) return;
                const dx = end - start;
                if (Math.abs(dx) < 40) return;
                goToSlide(slideIndex + (dx < 0 ? 1 : -1));
              }}
            >
              {HERO_SLIDES.map((s, i) => (
                <img
                  key={s.label}
                  src={s.image}
                  alt={i === slideIndex ? s.alt : ""}
                  aria-hidden={i !== slideIndex}
                  className={`hero-slide${i === slideIndex ? " is-active" : ""}`}
                  draggable={false}
                />
              ))}
              <div className="hero-shade" />
              <MobileTopBar brand />
              <div className="hero-copy">
                <div key={slide.label} className="hero-text">
                  <div className="eyebrow">{slide.eyebrow}</div>
                  <h2>
                    {slide.title[0]}
                    <br />
                    {slide.title[1]}
                  </h2>
                  <p>
                    {slide.desc[0]}
                    <br />
                    {slide.desc[1]}
                  </p>
                </div>
                <div className="hero-pagination">
                  {HERO_SLIDES.map((s, i) => (
                    <button
                      key={s.label}
                      type="button"
                      className={`hero-dot${i === slideIndex ? " is-active" : ""}`}
                      aria-label={`第 ${i + 1} 屏：${s.label}`}
                      aria-current={i === slideIndex}
                      onClick={() => goToSlide(i)}
                    >
                      <i />
                    </button>
                  ))}
                  <span className="hero-index">{slide.label}</span>
                </div>
              </div>
            </div>

            <div className="home-bottom">
              {homeCard?.kind === "draft" && (
                <button type="button" className="resume" onClick={handleResume}>
                  <span>
                    <span>上次的心动，还替你留着</span>
                    <br />
                    <small>
                      {draft.cityName || "城市探索"} · {draft.days || 3} 天 ·{" "}
                      {draft.sceneIds.length} 个心动画面
                    </small>
                  </span>
                  <Icon name="arrow" />
                </button>
              )}
              {homeCard && homeCard.kind !== "draft" && (
                <button
                  type="button"
                  className="resume"
                  onClick={() => handleOpenTask(homeCard.task)}
                >
                  <span>
                    <span>{TASK_CARD_COPY[homeCard.kind].title}</span>
                    <br />
                    <small>
                      {homeCard.task.destination} · {TASK_CARD_COPY[homeCard.kind].hint}
                    </small>
                  </span>
                  <Icon name="arrow" />
                </button>
              )}
              <div className="tiny-label">LET YOUR FEELINGS LEAD</div>
              <h3>这一次，想过怎样的日子？</h3>
              <p className="sub">把填选项，换成挑一段想拥有的生活。</p>
              <button
                type="button"
                className="primary home-entry"
                onClick={handleStartQuiz}
              >
                <span className="entry-left">
                  挑几个心动画面
                  <small>从场景选择，找到这次的旅行感觉</small>
                </span>
                <Icon name="spark" />
              </button>
              <div className="entry-grid">
                <button
                  type="button"
                  className="entry-tile"
                  onClick={() => navigate("/explore/inspiration")}
                >
                  <Icon name="layers" />
                  <strong>随手收集灵感</strong>
                  <small>看真实地点，收藏想去的地方</small>
                  <Icon name="arrow" className="corner" />
                </button>
                <button type="button" className="entry-tile" onClick={handleDirect}>
                  <Icon name="compass" />
                  <strong>已经想好去哪</strong>
                  <small>从你的目的地开始</small>
                  <Icon name="arrow" className="corner" />
                </button>
              </div>
              <div className="home-foot">
                <span>云途 · 让心动成为旅程</span>
                <span>游客即可规划</span>
              </div>
            </div>
          </div>
        </section>

        {directStage === "confirm" && (
          <MobileSheet title="开始新的旅程？" onClose={() => setDirectStage(null)}>
            <p>开始新的旅程会替换上次的草稿。</p>
            <button type="button" className="primary" onClick={() => setDirectStage("cities")}>
              <span>开始新的旅程</span>
              <Icon name="arrow" />
            </button>
            <button type="button" className="secondary" onClick={() => setDirectStage(null)}>
              保留上次的草稿
            </button>
          </MobileSheet>
        )}
        {directStage === "cities" && (
          <MobileLiveCityPicker onSelect={handleDirectCity} onClose={() => setDirectStage(null)} />
        )}

      </div>
    </div>
  );
};

export default MobileHomePage;


