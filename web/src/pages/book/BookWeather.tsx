/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect } from "react";
import {
  isReducedMotionPreferred,
  type BookMapWeatherVisual,
} from "@/utils/spreadBook";

export interface BookWeatherProps {
  AMap: any;
  map: any;
  containerEl: HTMLElement | null;
  visual: BookMapWeatherVisual;
}

interface RainDrop {
  x: number;
  y: number;
  l: number;
  v: number;
}

interface SnowFlake {
  x: number;
  y: number;
  r: number;
  v: number;
  s: number;
}

function measureStageSize(
  containerEl: HTMLElement,
  layerEl: HTMLElement,
): { width: number; height: number } {
  const cw = containerEl.clientWidth || layerEl.clientWidth || 0;
  const ch = containerEl.clientHeight || layerEl.clientHeight || 0;
  if (cw > 0 && ch > 0) {
    return { width: cw, height: ch };
  }
  const rect = containerEl.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0) {
    return { width: Math.round(rect.width), height: Math.round(rect.height) };
  }
  return { width: 1280, height: 720 };
}

/**
 * 地图上的天气图层：
 * 只在地图底部与路线虚线之上、站钉与地名之下挂载高德官方 AMap.CustomLayer，
 * 不改变高德底图颜色，并彻底 pointer-events: none。
 */
export function BookWeather({
  AMap,
  map,
  containerEl,
  visual,
}: BookWeatherProps) {
  useEffect(() => {
    if (
      !AMap ||
      !map ||
      !containerEl ||
      typeof AMap.CustomLayer !== "function"
    ) {
      return;
    }
    if (visual.kind === "none" || visual.kind === "sun") {
      return;
    }

    const layerEl = document.createElement("div");
    layerEl.className = "bk-wx-layer";
    layerEl.dataset.wx = visual.kind;
    if (visual.heavy) {
      layerEl.dataset.heavy = "true";
    }
    layerEl.setAttribute("aria-hidden", "true");
    layerEl.style.pointerEvents = "none";
    layerEl.style.width = "100%";
    layerEl.style.height = "100%";

    let reducedMotion = isReducedMotionPreferred();
    layerEl.classList.toggle("is-reduced", reducedMotion);

    const syncHostOrigin = () => {
      const host = layerEl.parentElement;
      if (!host || host === containerEl) return;
      if (host.style.left !== "0px") host.style.left = "0px";
      if (host.style.top !== "0px") host.style.top = "0px";
      if (host.style.transform !== "none") host.style.transform = "none";
    };

    const layer = new AMap.CustomLayer(layerEl, {
      zooms: [2, 20],
      zIndex: 80,
      alwaysRender: true,
      render: syncHostOrigin,
    });

    if (visual.kind === "cloud") {
      const cloudsWrap = document.createElement("div");
      cloudsWrap.className = "bk-wx-clouds";
      for (let i = 0; i < 3; i += 1) {
        const puff = document.createElement("span");
        puff.className = "bk-wx-cloud";
        cloudsWrap.appendChild(puff);
      }
      layerEl.appendChild(cloudsWrap);
      layer.setMap?.(map);
      return () => {
        layer.setMap?.(null);
      };
    }

    if (visual.kind === "fog") {
      const fogEl = document.createElement("div");
      fogEl.className = "bk-wx-fog";
      layerEl.appendChild(fogEl);
      layer.setMap?.(map);
      return () => {
        layer.setMap?.(null);
      };
    }

    // rain / snow：官方 CustomLayer 内铺随屏幕固定的 canvas
    const canvas = document.createElement("canvas");
    canvas.className = "bk-wx-canvas";
    canvas.style.pointerEvents = "none";
    layerEl.appendChild(canvas);
    layer.setMap?.(map);

    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = canvas.getContext("2d");
    } catch {
      ctx = null;
    }

    if (!ctx) {
      return () => {
        layer.setMap?.(null);
      };
    }

    let width = 1280;
    let height = 720;
    let dpr = 1;
    let drops: RainDrop[] = [];
    let flakes: SnowFlake[] = [];
    let rafId: number | null = null;
    let lastTs = 0;
    let disposed = false;

    const rebuildParticles = (prevW: number, prevH: number) => {
      if (visual.kind === "rain") {
        const density = visual.heavy ? 360 : 170;
        const targetCount = Math.max(
          48,
          Math.min(520, Math.round((density * width * height) / 1_200_000)),
        );
        if (drops.length === 0) {
          drops = Array.from({ length: targetCount }, () => ({
            x: Math.random() * width,
            y: Math.random() * height,
            l: (visual.heavy ? 16 : 12) + Math.random() * 12,
            v: (visual.heavy ? 13 : 8) + Math.random() * 6,
          }));
        } else {
          const sx = prevW > 0 ? width / prevW : 1;
          const sy = prevH > 0 ? height / prevH : 1;
          drops.forEach((d) => {
            d.x *= sx;
            d.y *= sy;
          });
          while (drops.length < targetCount) {
            drops.push({
              x: Math.random() * width,
              y: Math.random() * height,
              l: (visual.heavy ? 16 : 12) + Math.random() * 12,
              v: (visual.heavy ? 13 : 8) + Math.random() * 6,
            });
          }
          if (drops.length > targetCount) {
            drops.length = targetCount;
          }
        }
        return;
      }

      const density = visual.heavy ? 200 : 120;
      const targetCount = Math.max(
        36,
        Math.min(320, Math.round((density * width * height) / 1_200_000)),
      );
      if (flakes.length === 0) {
        flakes = Array.from({ length: targetCount }, () => ({
          x: Math.random() * width,
          y: Math.random() * height,
          r: 1.4 + Math.random() * 1.8,
          v: (visual.heavy ? 0.55 : 0.35) + Math.random() * 0.7,
          s: Math.random() * Math.PI * 2,
        }));
      } else {
        const sx = prevW > 0 ? width / prevW : 1;
        const sy = prevH > 0 ? height / prevH : 1;
        flakes.forEach((f) => {
          f.x *= sx;
          f.y *= sy;
        });
        while (flakes.length < targetCount) {
          flakes.push({
            x: Math.random() * width,
            y: Math.random() * height,
            r: 1.4 + Math.random() * 1.8,
            v: (visual.heavy ? 0.55 : 0.35) + Math.random() * 0.7,
            s: Math.random() * Math.PI * 2,
          });
        }
        if (flakes.length > targetCount) {
          flakes.length = targetCount;
        }
      }
    };

    const syncCanvasSize = () => {
      const next = measureStageSize(containerEl, layerEl);
      const prevW = width;
      const prevH = height;
      width = next.width;
      height = next.height;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
      rebuildParticles(prevW, prevH);
    };

    const drawFrame = (step: number) => {
      if (!ctx) return;
      ctx.clearRect(0, 0, width, height);

      if (visual.kind === "rain") {
        ctx.strokeStyle = visual.heavy
          ? "rgba(48, 72, 86, 0.42)"
          : "rgba(56, 82, 96, 0.36)";
        ctx.lineWidth = visual.heavy ? 1.2 : 1.05;
        ctx.lineCap = "round";
        ctx.beginPath();
        for (const d of drops) {
          ctx.moveTo(d.x, d.y);
          ctx.lineTo(d.x + d.l * 0.2, d.y + d.l);
          if (step > 0) {
            d.y += d.v * step;
            d.x += d.v * 0.2 * step;
            if (d.y > height) {
              d.y = -d.l;
              d.x = Math.random() * width;
            } else if (d.x > width + d.l) {
              d.x = -d.l;
            }
          }
        }
        ctx.stroke();
        return;
      }

      ctx.fillStyle = "rgba(255, 255, 255, 0.95)";
      ctx.strokeStyle = "rgba(96, 118, 128, 0.55)";
      ctx.lineWidth = 0.9;
      ctx.beginPath();
      for (const f of flakes) {
        if (step > 0) {
          f.s += 0.02 * step;
          f.y += f.v * step;
          f.x += Math.sin(f.s) * 0.35 * step;
          if (f.y > height + 4) {
            f.y = -4;
            f.x = Math.random() * width;
          }
          if (f.x > width + 4) {
            f.x = -4;
          } else if (f.x < -4) {
            f.x = width + 4;
          }
        }
        ctx.moveTo(f.x + f.r, f.y);
        ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2);
      }
      ctx.fill();
      ctx.stroke();
    };

    const stopLoop = () => {
      if (rafId !== null) {
        window.cancelAnimationFrame(rafId);
        rafId = null;
      }
      lastTs = 0;
    };

    const tick = (now: number) => {
      rafId = null;
      if (disposed || reducedMotion || document.hidden) return;
      const dt = lastTs > 0 ? now - lastTs : 16.67;
      lastTs = now;
      const step = Math.min(2.5, Math.max(0.25, dt / 16.67));
      drawFrame(step);
      rafId = window.requestAnimationFrame(tick);
    };

    const startLoopIfAllowed = () => {
      if (disposed || reducedMotion || document.hidden) return;
      if (rafId !== null) return;
      lastTs = 0;
      rafId = window.requestAnimationFrame(tick);
    };

    syncCanvasSize();
    drawFrame(0);
    startLoopIfAllowed();

    const handleVisibilityChange = () => {
      if (document.hidden) {
        stopLoop();
      } else {
        drawFrame(0);
        startLoopIfAllowed();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => {
        if (disposed) return;
        syncCanvasSize();
        drawFrame(0);
      });
      ro.observe(containerEl);
    }

    const motionMq =
      typeof window.matchMedia === "function"
        ? window.matchMedia("(prefers-reduced-motion: reduce)")
        : null;
    const handleMotionChange = (e: MediaQueryListEvent) => {
      reducedMotion = e.matches;
      layerEl.classList.toggle("is-reduced", reducedMotion);
      if (reducedMotion) {
        stopLoop();
        drawFrame(0);
      } else {
        startLoopIfAllowed();
      }
    };
    motionMq?.addEventListener?.("change", handleMotionChange);

    return () => {
      disposed = true;
      stopLoop();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      motionMq?.removeEventListener?.("change", handleMotionChange);
      ro?.disconnect();
      layer.setMap?.(null);
    };
  }, [AMap, map, containerEl, visual.kind, visual.heavy]);

  return null;
}
