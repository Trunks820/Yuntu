import React, { useState } from "react";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { computeEndDate, isDateInPast, isValidCalendarDate } from "@/utils/mobileDraft";
import type { MobileRhythm } from "@/types/mobile";
import { Icon, Logo, MobileSheet } from "./mobileUi";
import { findCachedDestination, formatMonthDay } from "./mobileFormat";

interface MobileDepartureStepProps {
  onNext: () => void;
}

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

/** 「随手收集灵感」不经过节奏页，在出发票上选填 */
const INSPIRATION_PACES: [MobileRhythm, string, string][] = [
  ["slow", "慢一点", "少安排几站，多留一会儿"],
  ["full", "充实一点", "多走几处，多体验一些"],
];

function localISO(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const RouteArt: React.FC = () => (
  <svg className="route-art" viewBox="0 0 100 36" aria-hidden="true">
    <path d="M2 22c21-28 58-25 92-9" strokeDasharray="3 4" />
    <path d="m77 10 17 3-11 11M94 13 75 22" />
  </svg>
);

export const MobileDepartureStep: React.FC<MobileDepartureStepProps> = ({
  onNext,
}) => {
  const draft = useMobileTripStore((s) => s.draft);
  const setDays = useMobileTripStore((s) => s.setDays);
  const setDateMode = useMobileTripStore((s) => s.setDateMode);
  const setStartDate = useMobileTripStore((s) => s.setStartDate);
  const setRhythm = useMobileTripStore((s) => s.setRhythm);
  const days = draft.days || 3;
  const hasExactDate =
    draft.dateMode === "exact" && !!draft.startDate && isValidCalendarDate(draft.startDate);
  const cityCode = findCachedDestination(draft.cityId, draft.cityName)?.nameEn || "";

  const [calendarOpen, setCalendarOpen] = useState(false);
  const [calendarYear, setCalendarYear] = useState(0);
  const [calendarMonth, setCalendarMonth] = useState(0); // 0-indexed
  const [tempSelectedDate, setTempSelectedDate] = useState<string | null>(null);

  const openCalendar = () => {
    const dt = hasExactDate ? new Date(draft.startDate + "T12:00:00") : new Date();
    setCalendarYear(dt.getFullYear());
    setCalendarMonth(dt.getMonth());
    setTempSelectedDate(hasExactDate ? draft.startDate : null);
    setCalendarOpen(true);
  };

  const shiftMonth = (delta: number) => {
    const dt = new Date(calendarYear, calendarMonth + delta, 1);
    setCalendarYear(dt.getFullYear());
    setCalendarMonth(dt.getMonth());
  };

  const handleConfirmDate = () => {
    if (!tempSelectedDate) return;
    setDateMode("exact");
    setStartDate(tempSelectedDate);
    setCalendarOpen(false);
  };

  const handleSetFlexibleDate = () => {
    setDateMode("undecided");
    setStartDate(null);
    setTempSelectedDate(null);
    setCalendarOpen(false);
  };

  const dateText = hasExactDate
    ? `${formatMonthDay(draft.startDate!)} — ${formatMonthDay(computeEndDate(draft.startDate!, days))}`
    : "日子还没定，先期待着";

  const renderCalendar = () => {
    const first = new Date(calendarYear, calendarMonth, 1);
    const count = new Date(calendarYear, calendarMonth + 1, 0).getDate();
    const offset = (first.getDay() + 6) % 7; // 周一为 0
    const today = localISO(new Date());
    const start = tempSelectedDate;
    const end = start ? computeEndDate(start, days) : null;

    return (
      <MobileSheet title="哪天出发？" onClose={() => setCalendarOpen(false)} closeLabel="关闭日历">
        <p>已选 {days} 天，点选出发日即可。</p>
        <div className="calendar-head">
          <button type="button" className="icon-btn" onClick={() => shiftMonth(-1)} aria-label="上个月">
            <Icon name="back" />
          </button>
          <span>
            {calendarYear} 年 {calendarMonth + 1} 月
          </span>
          <button type="button" className="icon-btn" onClick={() => shiftMonth(1)} aria-label="下个月">
            <Icon name="chevron" />
          </button>
        </div>
        <div className="calendar-grid">
          {WEEKDAYS.map((w) => (
            <div key={w} className="weekday">
              {w}
            </div>
          ))}
          {Array.from({ length: offset }, (_, i) => (
            <span key={`empty-${i}`} className="empty" />
          ))}
          {Array.from({ length: count }, (_, i) => {
            const date = localISO(new Date(calendarYear, calendarMonth, i + 1));
            const cls = [
              date === start ? "selected" : start && end && date > start && date <= end ? "ticket-range" : "",
              date === today ? "today" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <button
                key={date}
                type="button"
                className={cls || undefined}
                disabled={isDateInPast(date)}
                aria-pressed={date === start}
                aria-label={formatMonthDay(date, true)}
                onClick={() => setTempSelectedDate(date)}
              >
                {i + 1}
              </button>
            );
          })}
        </div>
        <div className="calendar-hint" aria-live="polite">
          {start && end
            ? `${formatMonthDay(start, true)} — ${formatMonthDay(end, true)} · 共 ${days} 天`
            : "点选一天，作为这次旅程的开始。"}
        </div>
        <button
          type="button"
          className="primary"
          onClick={handleConfirmDate}
          disabled={!tempSelectedDate}
        >
          <span>就从这天出发</span>
          <Icon name="arrow" />
        </button>
        <button type="button" className="secondary" onClick={handleSetFlexibleDate}>
          暂不确定日期
        </button>
      </MobileSheet>
    );
  };

  return (
    <>
      <div className="scroll">
        <div className="screen-inner page-pad">
          <div className="eyebrow">02 / A LITTLE TIME, JUST FOR YOU</div>
          <h2 className="page-title">
            给这段心动，
            <br />
            留一张<em>出发票</em>。
          </h2>
          <p className="sub">先选玩几天，出发日期可以晚点决定。</p>

          <article className="ticket" aria-label="旅行出发票">
            <div className="ticket-head">
              <Logo />
              <small>NOT A BOOKING / 旅行草稿</small>
            </div>
            <div className="ticket-main">
              <div className="ticket-route">
                <div>
                  <div className="ticket-city">{draft.cityName || "待选目的地"}</div>
                  <div className="ticket-code">{cityCode.toUpperCase()}</div>
                </div>
                <RouteArt />
                <div className="ticket-days">
                  <strong>{String(days).padStart(2, "0")}</strong>
                  <small>天</small>
                  <div className="ticket-code">YOUR DAYS</div>
                </div>
              </div>
            </div>
            <div className="ticket-perforation" />
            <button
              type="button"
              className="ticket-bottom ticket-date-button"
              onClick={openCalendar}
              aria-label={`选择出发日期，${hasExactDate ? dateText : "日期未定"}`}
            >
              <span>
                <span className="tiny" style={{ display: "block" }}>
                  出发日期（选填）
                </span>
                <span className="ticket-date">{dateText}</span>
              </span>
              <Icon name="pen" />
            </button>
          </article>

          <div className="section-kicker">
            <span>给自己留几天？</span>
            <span>1–7 天</span>
          </div>
          <div className="duration-rail ticket-duration" role="radiogroup" aria-label="行程天数">
            {[1, 2, 3, 4, 5, 6, 7].map((n) => (
              <button
                key={n}
                type="button"
                role="radio"
                aria-checked={days === n}
                aria-label={`${n}天`}
                className={`duration-card${days === n ? " selected" : ""}`}
                onClick={() => setDays(n)}
              >
                <strong>{String(n).padStart(2, "0")}</strong>
                <small>天</small>
              </button>
            ))}
          </div>
          {draft.entry === "inspiration" && (
            <fieldset className="inspiration-pace">
              <legend>
                旅行节奏 <span>（选填）</span>
              </legend>
              <div>
                {INSPIRATION_PACES.map(([key, label, sub]) => (
                  <button
                    key={key}
                    type="button"
                    className={`pace-option${draft.rhythm === key ? " selected" : ""}`}
                    aria-pressed={draft.rhythm === key}
                    onClick={() => setRhythm(draft.rhythm === key ? null : key)}
                  >
                    <strong>{label}</strong>
                    <small>{sub}</small>
                  </button>
                ))}
              </div>
              <p>{draft.rhythm ? "再点一次可以取消选择。" : "不选也可以，节奏先不限定。"}</p>
            </fieldset>
          )}
          <p className="ticket-optional">
            只选天数，也可以继续。
            <br />
            想好哪天出发，再点票面补上。
          </p>
        </div>
      </div>

      <div className="footer-action">
        <button type="button" className="primary" onClick={onNext}>
          <span>
            {draft.editReturn === "review" ? "把出发票放回旅行册" : "出发票收好，下一页"}
          </span>
          <Icon name="arrow" />
        </button>
      </div>

      {calendarOpen && renderCalendar()}
    </>
  );
};


