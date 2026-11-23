/**
 * 手机端旅行册左右滑换天的纯判定函数。
 * 保持无 DOM 依赖，便于独立测试手势边界与换天决策。
 */

export const SWIPE_EDGE_GUARD_PX = 24;
export const SWIPE_LOCK_DISTANCE_PX = 10;
export const SWIPE_HORIZONTAL_RATIO = 1.3;
export const SWIPE_FOLLOW_FACTOR = 0.55;
export const SWIPE_EDGE_RESISTANCE_FACTOR = 0.18;
export const SWIPE_SWITCH_THRESHOLD_PX = 60;

export type SwipeAxisDecision = "pending" | "horizontal" | "vertical";

export interface SwipeFollowResult {
  offsetX: number;
  opacity: number;
  atBoundary: boolean;
}

export interface DaySwipeResolution {
  shouldSwitch: boolean;
  targetIndex: number;
  direction: -1 | 0 | 1;
}

/** 起点落在屏幕左右边缘 24px 内时留给系统返回手势，不响应横滑。 */
export function isSwipeStartInEdgeZone(
  clientX: number,
  viewportWidth: number,
  edgePx = SWIPE_EDGE_GUARD_PX,
): boolean {
  if (
    !Number.isFinite(clientX) ||
    !Number.isFinite(viewportWidth) ||
    viewportWidth <= 0
  ) {
    return true;
  }
  return clientX < edgePx || clientX > viewportWidth - edgePx;
}

/** 移动距离超过 10px 才判定方向；横向位移大于纵向 1.3 倍才算横滑。 */
export function decideSwipeAxis(
  dx: number,
  dy: number,
  lockDistancePx = SWIPE_LOCK_DISTANCE_PX,
  horizontalRatio = SWIPE_HORIZONTAL_RATIO,
): SwipeAxisDecision {
  if (Math.hypot(dx, dy) <= lockDistancePx) {
    return "pending";
  }
  return Math.abs(dx) > Math.abs(dy) * horizontalRatio
    ? "horizontal"
    : "vertical";
}

/** 计算横滑跟手位移与轻淡透明度：正常 ×0.55，首日右拉或末日左拉 ×0.18。 */
export function computeSwipeFollow(
  dx: number,
  currentIndex: number,
  totalDays: number,
): SwipeFollowResult {
  const atBoundary =
    totalDays <= 1 ||
    (dx > 0 && currentIndex <= 0) ||
    (dx < 0 && currentIndex >= totalDays - 1);
  const offsetX =
    dx * (atBoundary ? SWIPE_EDGE_RESISTANCE_FACTOR : SWIPE_FOLLOW_FACTOR);
  const opacity = 1 - Math.min(0.35, Math.abs(offsetX) / 600);
  return { offsetX, opacity, atBoundary };
}

export const SHEET_CLOSE_DISTANCE_RATIO = 1 / 3;
export const SHEET_CLOSE_VELOCITY_PX_PER_MS = 0.6;
export const SHEET_CLOSE_MIN_FLING_PX = 30;

/**
 * 地点详情弹层按住顶部横杠下拉后松手：拖过弹层高度的 1/3，
 * 或甩得够快（> 0.6px/ms）且已拖出 30px 以上，就关闭；否则弹回。
 */
export function shouldCloseSheetOnRelease(
  dy: number,
  durationMs: number,
  sheetHeight: number,
): boolean {
  if (!Number.isFinite(dy) || dy <= 0) return false;
  if (sheetHeight > 0 && dy > sheetHeight * SHEET_CLOSE_DISTANCE_RATIO) {
    return true;
  }
  const velocity = dy / Math.max(1, durationMs);
  return (
    velocity > SHEET_CLOSE_VELOCITY_PX_PER_MS && dy > SHEET_CLOSE_MIN_FLING_PX
  );
}

/**
 * 松手后决定该不该换天、切到哪一天：
 * 左滑（dx <= -60）切下一天（direction = 1）；右滑（dx >= 60）切上一天（direction = -1）。
 */
export function resolveDaySwipe(
  dx: number,
  currentIndex: number,
  totalDays: number,
  thresholdPx = SWIPE_SWITCH_THRESHOLD_PX,
): DaySwipeResolution {
  if (totalDays <= 1 || currentIndex < 0 || currentIndex >= totalDays) {
    return { shouldSwitch: false, targetIndex: currentIndex, direction: 0 };
  }
  if (dx <= -thresholdPx && currentIndex < totalDays - 1) {
    return {
      shouldSwitch: true,
      targetIndex: currentIndex + 1,
      direction: 1,
    };
  }
  if (dx >= thresholdPx && currentIndex > 0) {
    return {
      shouldSwitch: true,
      targetIndex: currentIndex - 1,
      direction: -1,
    };
  }
  return { shouldSwitch: false, targetIndex: currentIndex, direction: 0 };
}
