import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { WeatherDay } from "@/types/trip";
import {
  buildWeatherReminderContent,
  computeSunBubblePosition,
  formatBookDayLabel,
  isReducedMotionPreferred,
  pickSunDropPlacement,
  type SunBubblePosition,
  type SunDropPoint,
} from "@/utils/spreadBook";

export interface BookSunDropProps {
  weatherDay: WeatherDay;
  stageWidth: number;
  stageHeight: number;
  isReading: boolean;
  avoidPoints: readonly SunDropPoint[];
  collecting: boolean;
  targetIconSelector: string;
  onCollect: () => void;
}

const DEFAULT_STAGE_RECT = { left: 0, top: 0, width: 1440, height: 848 };
// 匀速直落，照植物大战僵尸天上掉阳光的手感：一路晃着落，落地弹一下，点了先鼓一下再飞走
const SUN_FALL_SPEED_PX_PER_S = 130;
const SUN_FALL_MIN_MS = 2600;
const SUN_FALL_MAX_MS = 5500;
const SUN_LAND_BOUNCE_MS = 520;
// 手画感的十六道短光芒：长短不齐、角度略偏
const SUN_RAY_LENGTHS = [9, 6, 8, 5, 9, 6, 7, 5, 9, 6, 8, 5, 9, 7, 8, 5];

/**
 * 晴天从地图上沿匀速直落的那颗太阳：样子是纸上的一枚金箔，手感参考植物大战僵尸的阳光。
 * 地上先有一团淡影，越落越小越实；真按钮、下落途中也能点，收下后飞进当前可见那一天页签上的天气图标里。
 */
export function BookSunDrop({
  weatherDay,
  stageWidth,
  stageHeight,
  isReading,
  avoidPoints,
  collecting,
  targetIconSelector,
  onCollect,
}: BookSunDropProps) {
  const btnRef = useRef<HTMLButtonElement>(null);
  const shadowRef = useRef<HTMLSpanElement>(null);
  const flyAnimRef = useRef<Animation | null>(null);
  const gradientId = useId();
  const [landed, setLanded] = useState(false);
  // 点下那一刻太阳在屏幕上的位置（可能还在半空），收的时候从这里起飞
  const collectFromRef = useRef<{ x: number; y: number } | null>(null);
  const avoidPointsRef = useRef(avoidPoints);
  avoidPointsRef.current = avoidPoints;

  // 落点一开始就让开右边详情会打开的位置；之后选站、开合详情都不再挪，只在舞台尺寸或读攻略状态变了才重选
  const placement = useMemo(
    () =>
      pickSunDropPlacement(
        stageWidth,
        stageHeight,
        true,
        isReading,
        avoidPointsRef.current,
        weatherDay.day,
      ),
    [stageWidth, stageHeight, isReading, weatherDay.day],
  );

  // 视野落定后从舞台上沿匀速直落，落地弹一下；减弱动态时直接停在落点
  useEffect(() => {
    const btn = btnRef.current;
    if (!btn || collecting) return;
    if (isReducedMotionPreferred() || typeof btn.animate !== "function") {
      setLanded(true);
      return;
    }
    setLanded(false);

    const dy0 = placement.start.y - placement.target.y;
    const duration = Math.round(
      Math.max(
        SUN_FALL_MIN_MS,
        Math.min(
          SUN_FALL_MAX_MS,
          (Math.abs(dy0) / SUN_FALL_SPEED_PX_PER_S) * 1000,
        ),
      ),
    );

    const anims: Animation[] = [];
    let cancelled = false;
    try {
      const fall = btn.animate(
        [
          { transform: `translate(0px, ${dy0}px)` },
          { transform: "translate(0px, 0px)" },
        ],
        { duration, easing: "linear", fill: "none" },
      );
      anims.push(fall);
      const shadow = shadowRef.current;
      if (shadow && typeof shadow.animate === "function") {
        anims.push(
          shadow.animate(
            [
              { transform: "scale(2.4)", opacity: 0.15 },
              { transform: "scale(1)", opacity: 1 },
            ],
            { duration, easing: "linear", fill: "none" },
          ),
        );
      }
      fall.onfinish = () => {
        if (cancelled) return;
        setLanded(true);
        anims.push(
          btn.animate(
            [
              { transform: "scale(1.16, 0.8)" },
              {
                offset: 0.45,
                transform: "translate(0px, -14px) scale(0.95, 1.06)",
              },
              { offset: 0.75, transform: "scale(1.05, 0.95)" },
              { transform: "scale(1)" },
            ],
            { duration: SUN_LAND_BOUNCE_MS, easing: "ease-out", fill: "none" },
          ),
        );
      };
    } catch {
      // jsdom 或部分环境未完整实现 Web Animations API 时直接停在目标位置
      setLanded(true);
    }

    return () => {
      cancelled = true;
      anims.forEach((anim) => anim.cancel?.());
    };
  }, [collecting, placement.start.y, placement.target.x, placement.target.y]);

  // 点击收集：原地鼓一下，再从当前位置飞向当前可见页签的天气图标，越飞越小（~0.72s）
  useEffect(() => {
    const btn = btnRef.current;
    if (!collecting || !btn) return;

    if (isReducedMotionPreferred() || typeof btn.animate !== "function") {
      return;
    }

    const atlasEl = btn.closest(".bk-atlas");
    const targetIcon =
      atlasEl?.querySelector<HTMLElement>(targetIconSelector) ??
      document.querySelector<HTMLElement>(targetIconSelector);

    const stageRect = atlasEl?.getBoundingClientRect() ?? {
      ...DEFAULT_STAGE_RECT,
      right: stageWidth,
      bottom: stageHeight,
    };
    const from = collectFromRef.current;
    const curDx = from ? from.x - stageRect.left - placement.target.x : 0;
    const curDy = from ? from.y - stageRect.top - placement.target.y : 0;

    const iconRect = targetIcon?.getBoundingClientRect();
    const hasIconLayout =
      Boolean(iconRect) &&
      ((iconRect?.width ?? 0) > 0 || (iconRect?.height ?? 0) > 0);
    const destCenterX = hasIconLayout
      ? iconRect!.left + iconRect!.width / 2 - stageRect.left
      : placement.target.x;
    const destCenterY = hasIconLayout
      ? iconRect!.top + iconRect!.height / 2 - stageRect.top
      : stageHeight - 200;
    const destDx = destCenterX - placement.target.x;
    const destDy = destCenterY - placement.target.y;

    try {
      const anim = btn.animate(
        [
          {
            offset: 0,
            transform: `translate(${curDx}px, ${curDy}px) scale(1)`,
            opacity: 1,
            easing: "ease-out",
          },
          {
            offset: 0.16,
            transform: `translate(${curDx}px, ${curDy}px) scale(1.28)`,
            opacity: 1,
            easing: "cubic-bezier(0.5, 0, 0.3, 1)",
          },
          {
            offset: 0.88,
            transform: `translate(${Math.round(curDx + (destDx - curDx) * 0.94)}px, ${Math.round(curDy + (destDy - curDy) * 0.94)}px) scale(0.42)`,
            opacity: 1,
          },
          {
            offset: 1,
            transform: `translate(${destDx}px, ${destDy}px) scale(0.3)`,
            opacity: 0,
          },
        ],
        {
          duration: 720,
          easing: "linear",
          fill: "forwards",
        },
      );
      flyAnimRef.current = anim;
    } catch {
      // ignore
    }

    return () => {
      flyAnimRef.current?.cancel?.();
      flyAnimRef.current = null;
    };
  }, [
    collecting,
    placement.target.x,
    placement.target.y,
    stageHeight,
    stageWidth,
    targetIconSelector,
  ]);

  const ariaLabel = `${formatBookDayLabel(weatherDay.day)} ${weatherDay.weather_text}，收下这颗太阳看天气提醒`;

  const spot = {
    left: `${placement.target.x}px`,
    top: `${placement.target.y}px`,
  };
  const stateClass = collecting ? " is-collecting" : landed ? " is-landed" : "";

  return (
    <>
      <span
        ref={shadowRef}
        className={`bk-sdrop-shadow${stateClass}`}
        aria-hidden="true"
        style={spot}
      />
      <button
        ref={btnRef}
        type="button"
        className={`bk-sdrop${stateClass}`}
        aria-label={ariaLabel}
        disabled={collecting}
        style={spot}
        onClick={() => {
          const rect = btnRef.current?.getBoundingClientRect();
          collectFromRef.current =
            rect && rect.width > 0
              ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
              : null;
          onCollect();
        }}
      >
        <svg className="bk-sdrop-svg" viewBox="0 0 72 72" aria-hidden="true">
          <defs>
            <radialGradient id={gradientId} cx="0.42" cy="0.38" r="0.8">
              <stop offset="0" stopColor="#fbe08a" />
              <stop offset="0.7" stopColor="#efbd45" />
              <stop offset="1" stopColor="#e2a52e" />
            </radialGradient>
          </defs>
          <g className="bk-sdrop-body">
            <g className="bk-sdrop-rays">
              {SUN_RAY_LENGTHS.map((len, i) => (
                <line
                  key={i}
                  transform={`rotate(${i * 22.5 + (i % 3) * 1.6} 36 36)`}
                  x1="36"
                  y1="12"
                  x2="36"
                  y2={12 - len}
                  stroke="#c48a1c"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                />
              ))}
            </g>
            <circle
              cx="36"
              cy="36"
              r="19"
              fill={`url(#${gradientId})`}
              stroke="#c48a1c"
              strokeWidth="1.6"
            />
            <path
              d="M25 31a12 12 0 0 1 12-9"
              fill="none"
              stroke="#fff6d2"
              strokeWidth="2.2"
              strokeLinecap="round"
              opacity="0.9"
            />
          </g>
        </svg>
      </button>
    </>
  );
}

export interface BookSunBubbleProps {
  weatherDay: WeatherDay;
  targetIconSelector: string;
}

/**
 * 太阳飞进页签天气图标后冒出的当天天气与提醒卡片：
 * 第一行“第几天 · weather_text min–max°C”，风力非空另起一行，后接后端原始 reminders 与高温提醒。
 */
export function BookSunBubble({
  weatherDay,
  targetIconSelector,
}: BookSunBubbleProps) {
  const bubbleRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<SunBubblePosition>({
    left: 24,
    top: 220,
    placement: "above",
    arrowLeft: 28,
  });

  const content = useMemo(
    () => buildWeatherReminderContent(weatherDay),
    [weatherDay],
  );

  useLayoutEffect(() => {
    const syncPosition = () => {
      const bubbleEl = bubbleRef.current;
      const atlasEl = bubbleEl?.closest(".bk-atlas");
      if (!atlasEl) return;
      const stageRect = atlasEl.getBoundingClientRect();
      const targetIcon =
        atlasEl.querySelector<HTMLElement>(targetIconSelector) ??
        document.querySelector<HTMLElement>(targetIconSelector);
      if (!targetIcon) return;
      const iconRect = targetIcon.getBoundingClientRect();
      if (
        stageRect.width === 0 &&
        stageRect.height === 0 &&
        iconRect.width === 0 &&
        iconRect.height === 0
      ) {
        return;
      }
      setPos(computeSunBubblePosition(iconRect, stageRect, 270));
    };

    syncPosition();
    window.addEventListener("resize", syncPosition);
    return () => {
      window.removeEventListener("resize", syncPosition);
    };
  }, [targetIconSelector, weatherDay.day]);

  return (
    <div
      ref={bubbleRef}
      className="bk-sbub"
      role="status"
      aria-live="polite"
      aria-label="当天天气提醒"
      data-placement={pos.placement}
      style={{
        left: `${pos.left}px`,
        top: `${pos.top}px`,
        ["--bk-sbub-arrow-left" as string]: `${pos.arrowLeft}px`,
      }}
    >
      <b className="bk-sbub-head">{content.headline}</b>
      {content.windLine && (
        <span className="bk-sbub-wind">{content.windLine}</span>
      )}
      {content.reminders.map((rem) => (
        <span key={rem} className="bk-sbub-rem">
          {rem}
        </span>
      ))}
    </div>
  );
}
