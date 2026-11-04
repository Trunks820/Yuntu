import React, { Suspense, lazy, useState } from "react";
import type {
  CommuteLeg,
  PackingChecklistGroup,
  TravelTip,
  TripDay,
  TripPlan,
  TripResult,
  WeatherDay,
} from "@/types/trip";
import { exactTimeSourceLabel, governedExactTime, periodLabel } from "@/utils/schedule";
import { cleanSummary, cleanTags } from "@/utils/format";
import { categoryName } from "@/constants/places";
import { SafeDeliveryNotice } from "@/components/result/SafeDeliveryNotice";
import { MustIncludeNotice } from "@/components/detail/MustIncludeNotice";
import { CONCEPT_ASSETS } from "@/assets/mobile-concepts/conceptImages";
import { Icon } from "@/components/mobile/mobileUi";
import { MobileLodgingRow } from "@/pages/mobile/MobileLodgingRow";
import "@/styles/mobile.css";

const MapView = lazy(() =>
  import("@/components/detail/MapView").then((m) => ({ default: m.MapView })),
);

const PACE_LABEL: Record<string, string> = {
  RELAXED: "节奏轻松",
  MODERATE: "节奏适中",
  INTENSIVE: "节奏紧凑",
  PACKED: "节奏紧凑",
};

const COMMUTE_LABEL: Record<string, string> = {
  driving: "驾车",
  transit: "公共交通",
  walking: "步行",
  cycling: "骑行",
};

function commuteText(leg: CommuteLeg): string {
  const km = leg.distance_meters >= 1000
    ? `${(leg.distance_meters / 1000).toFixed(1)} 公里`
    : `${leg.distance_meters} 米`;
  const head = leg.mode === "transit" && leg.transit_summary
    ? leg.transit_summary
    : COMMUTE_LABEL[leg.mode] || "前往下一站";
  return `${head} · 约 ${leg.duration_minutes} 分钟 · ${km}`;
}

function stayText(minutes?: number): string | null {
  if (!minutes || minutes <= 0) return null;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `建议停留 ${h} 小时${m ? `${m} 分` : ""}` : `建议停留 ${m} 分钟`;
}

export interface MobilePlanViewProps {
  result: TripResult;
  plan: TripPlan;
  jobId: string | null;
  cityCovers: string[];
  weatherDays?: WeatherDay[];
  timePrefText: string | null;
  packing: PackingChecklistGroup[];
  tips: TravelTip[];
  /** 现有的大交通与费用组件，直接嵌入 */
  transportSlot: React.ReactNode;
  costSlot: React.ReactNode;
  pdfLoading: boolean;
  onPdf: () => void;
  onShare: () => void;
  onBack: () => void;
  onOpenPlace: (placeId: number) => void;
}

/** 手机端旅行册：原型 .result 版式，内容全部来自真实生成结果 */
export const MobilePlanView: React.FC<MobilePlanViewProps> = ({
  result,
  plan,
  jobId,
  cityCovers,
  weatherDays,
  timePrefText,
  packing,
  tips,
  transportSlot,
  costSlot,
  pdfLoading,
  onPdf,
  onShare,
  onBack,
  onOpenPlace,
}) => {
  const [dayNumber, setDayNumber] = useState(plan.days[0]?.day ?? 1);
  const [mapOpen, setMapOpen] = useState(false);
  const day: TripDay | undefined = plan.days.find((d) => d.day === dayNumber) ?? plan.days[0];
  const city = result.city.name;
  const cover = cityCovers[0] || CONCEPT_ASSETS.lake;
  const dayCover = cityCovers[(day ? plan.days.indexOf(day) : 0) % Math.max(1, cityCovers.length)] || cover;
  const weather = weatherDays?.find((w) => w.day === day?.day);
  const summary = cleanSummary(plan.summary);
  const tags = cleanTags(plan.tags);
  // E1：travel 的攻略名形如「{城市}{天数}日｜…」，本身已带城市名；
  // 标题里已有城市时不再单独排一行城市，避免同一座城出现两遍。
  const titleMentionsCity = city.length > 0 && plan.title.includes(city);

  return (
    <div className="ym">
      <div className="ym-device">
        <section className="screen result" aria-label={`${city} · 旅行册`}>
          <div className="scroll">
            <div className="result-hero">
              <img src={cover} alt={`${city} · 旅行册封面`} draggable={false} />
              <div className="topbar">
                <button type="button" className="icon-btn soft" onClick={onBack} aria-label="返回">
                  <Icon name="back" />
                </button>
                <span className="top-caption" style={{ color: "#ffffffc9" }}>
                  云途 · 你的旅行册
                </span>
                <button type="button" className="icon-btn soft" onClick={onShare} aria-label="分享旅行册">
                  <Icon name="spark" />
                </button>
              </div>
              <div className="result-hero-copy">
                <div className="eyebrow">YOUR PERSONAL TRAVEL BOOK</div>
                <h2>
                  {/* 城市与攻略名各占一行；断行交给 .result-hero-* 的 word-break:keep-all */}
                  {!titleMentionsCity && <span className="result-hero-city">{city}。</span>}
                  <span className="result-hero-plan">{plan.title}</span>
                </h2>
                <div className="result-meta">
                  <span>{result.request.days} 天</span>
                  <span className="dot" />
                  <span>{result.request.people_count} 人同行</span>
                  <span className="dot" />
                  <span>{PACE_LABEL[plan.pace.level] || "节奏适中"}</span>
                </div>
              </div>
            </div>

            <div className="result-body">
              {summary && <p className="result-summary">{summary}</p>}
              {(tags.length > 0 || timePrefText) && (
                <div className="tag-row" style={{ marginBottom: 16 }}>
                  {tags.map((t) => (
                    <span key={t} className="chip">
                      {t}
                    </span>
                  ))}
                  {timePrefText && <span className="chip">{timePrefText}</span>}
                </div>
              )}

              <div className="result-notices">
                <SafeDeliveryNotice result={result} jobId={jobId} />
                <MustIncludeNotice items={result.must_include} />
              </div>

              <div className="day-tabs" aria-label="按天查看旅行册">
                {plan.days.map((d) => (
                  <button
                    key={d.day}
                    type="button"
                    className={`day-tab${d.day === day?.day ? " selected" : ""}`}
                    aria-pressed={d.day === day?.day}
                    onClick={() => setDayNumber(d.day)}
                  >
                    DAY<b>{String(d.day).padStart(2, "0")}</b>
                  </button>
                ))}
              </div>

              {day && (
                <>
                  {/* E3：城市参考图放在当天内容顶部并标清身份，不再落在第 1 站下面冒充景点照片 */}
                  <div className="day-cover">
                    <div className="day-cover-frame">
                      <img
                        src={dayCover}
                        alt={`${city} · 城市参考图`}
                        loading="lazy"
                        draggable={false}
                      />
                    </div>
                    <small>城市参考图 · {city}</small>
                  </div>
                  <div className="day-heading">
                    <h3>{day.title || `第 ${day.day} 天`}</h3>
                    <small>
                      {weather ? `${weather.weather_text} ${weather.temp_min_c}–${weather.temp_max_c}°C · ` : ""}
                      CHAPTER {String(day.day).padStart(2, "0")}
                    </small>
                  </div>
                  {day.narrative && <p className="result-summary">{day.narrative}</p>}

                  <div className="day-line">
                    {day.places.map((place, i) => {
                      const exact = governedExactTime(place.schedule);
                      const period = periodLabel(place.schedule?.period);
                      const leg = day.commute_legs.find((l) => l.from_place_id === place.place_id);
                      const stay = stayText(place.stay_minutes);
                      return (
                        <article key={`${place.place_id}-${i}`} className="day-stop">
                          <div className="stop-top">
                            <strong>{exact ? exact.text : period || `第 ${i + 1} 站`}</strong>
                            <span>
                              {exact ? `${exactTimeSourceLabel(exact.source)} · ` : ""}
                              {categoryName(place.category)}
                            </span>
                          </div>
                          <button type="button" className="stop-title" onClick={() => onOpenPlace(place.place_id)}>
                            <h4>{place.name}</h4>
                          </button>
                          {(place.activity_note || place.brief) && <p>{place.activity_note || place.brief}</p>}
                          <div className="tag-row">
                            {stay && (
                              <span className="reason">
                                <Icon name="clock" />
                                {stay}
                              </span>
                            )}
                            {place.optional && (
                              <span className="reason">
                                <Icon name="spark" />
                                可选，看当天心情
                              </span>
                            )}
                          </div>
                          {leg && i < day.places.length - 1 && (
                            <p className="stop-commute">
                              <Icon name="arrow" />
                              {commuteText(leg)}
                            </p>
                          )}
                        </article>
                      );
                    })}
                  </div>
                  {day.commute_summary && <p className="result-disclaimer">{day.commute_summary}</p>}
                  {plan.accommodation && (
                    <MobileLodgingRow accommodation={plan.accommodation} />
                  )}
                </>
              )}

              {(packing.length > 0 || tips.length > 0) && (
                <>
                  <div className="day-heading" style={{ marginTop: 24 }}>
                    <h3>出发前，记得这些</h3>
                    <small>BEFORE YOU GO</small>
                  </div>
                  {packing.map((g) => (
                    <div key={g.category} className="manifest-row">
                      <Icon name="book" />
                      <span>
                        <b>{g.category}</b>：{g.items.join("、")}
                      </span>
                    </div>
                  ))}
                  {tips.map((t) => (
                    <div key={t.title} className="manifest-row">
                      <Icon name="info" />
                      <span>
                        <b>{t.title}</b>：{t.content}
                      </span>
                    </div>
                  ))}
                </>
              )}

              <div className="day-heading" style={{ marginTop: 24 }}>
                <h3>出行与费用</h3>
                <small>TRAVEL & COST</small>
              </div>
              <div className="result-slot">
                {transportSlot}
                {costSlot}
              </div>

              <div className="result-disclaimer">
                地点、开放时间与费用以实际情况为准，出发前建议再确认一次。
              </div>
            </div>
          </div>

          <div className="footer-action">
            <div className="result-footer">
              <button type="button" className="small-secondary" onClick={() => setMapOpen(true)}>
                查看地图
              </button>
              <button type="button" className="primary" onClick={onPdf} disabled={pdfLoading}>
                <span>{pdfLoading ? "正在导出…" : "收好这本旅行册（PDF）"}</span>
                <Icon name="arrow" />
              </button>
            </div>
          </div>
        </section>

        {mapOpen && day && (
          <div className="overlay map-overlay" role="dialog" aria-modal="true" aria-label={`第 ${day.day} 天地图`}>
            <section className="sheet map-sheet">
              <div className="sheet-title">
                <h3>第 {day.day} 天 · 路线</h3>
                <button type="button" className="icon-btn" onClick={() => setMapOpen(false)} aria-label="关闭地图">
                  <Icon name="close" />
                </button>
              </div>
              <div className="map-frame">
                <Suspense fallback={<p className="u-demo-note">地图加载中…</p>}>
                  <MapView
                    day={day}
                    days={plan.days}
                    selectedDay={day.day}
                    accommodation={plan.accommodation}
                    activePlaceId={null}
                    onMarkerClick={(id) => {
                      setMapOpen(false);
                      onOpenPlace(id);
                    }}
                  />
                </Suspense>
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  );
};


