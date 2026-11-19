import { useMemo } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useTripStore } from "@/stores/tripStore";
import { useTripTaskStore } from "@/stores/tripTaskStore";
import "@/styles/spread.css";

export type SpreadNavSection = "depart" | "book" | "trips";

function resolveActiveSection(pathname: string): SpreadNavSection {
  if (pathname === "/history") return "trips";
  if (
    pathname.startsWith("/planning/") ||
    pathname.startsWith("/result/") ||
    pathname.startsWith("/plan/")
  ) {
    return "book";
  }
  return "depart";
}

function buildResultHref(resultId: string | number, jobId?: string | null): string {
  const base = `/result/${encodeURIComponent(String(resultId))}`;
  const cleanJobId = jobId ? String(jobId).trim() : "";
  return cleanJobId ? `${base}?job_id=${encodeURIComponent(cleanJobId)}` : base;
}

function ArrowRightIcon() {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

export function SpreadTopBar() {
  const location = useLocation();
  const navigate = useNavigate();

  const tasks = useTripTaskStore((s) => s.tasks);
  const budgets = useTripTaskStore((s) => s.budgets);
  const acknowledgeTask = useTripTaskStore((s) => s.acknowledgeTask);

  const currentResult = useTripStore((s) => s.result);
  const currentJobId = useTripStore((s) => s.currentJobId);
  const stageProgress = useTripStore((s) => s.stageProgress);

  const activeSection = resolveActiveSection(location.pathname);

  const latestTask = useMemo(() => {
    const list = Object.values(tasks);
    if (list.length === 0) return null;
    const pending = list.find((t) => t.status === "pending");
    if (pending) return pending;
    const unread = list
      .filter((t) => t.notificationState === "unread")
      .sort((a, b) => b.startedAt - a.startedAt)[0];
    if (unread) return unread;
    return list.sort((a, b) => b.startedAt - a.startedAt)[0] ?? null;
  }, [tasks]);

  const bookHref = useMemo(() => {
    if (activeSection === "book") {
      return `${location.pathname}${location.search}`;
    }
    if (latestTask) {
      if (latestTask.status === "pending") {
        return `/planning/${latestTask.jobId}`;
      }
      if (latestTask.status === "ready" && latestTask.resultRecordId) {
        return buildResultHref(latestTask.resultRecordId, latestTask.jobId);
      }
    }
    if (currentResult?.resultId) {
      return buildResultHref(currentResult.resultId, currentResult.jobId);
    }
    if (currentJobId) {
      return `/planning/${currentJobId}`;
    }
    return "/history";
  }, [
    activeSection,
    location.pathname,
    location.search,
    latestTask,
    currentResult,
    currentJobId,
  ]);

  const bindChip = useMemo(() => {
    if (activeSection === "book") return null;

    // 1. Running task in tripTaskStore
    const runningTask = Object.values(tasks).find((t) => t.status === "pending");
    if (runningTask) {
      const city = runningTask.destination?.trim() || "行程";
      const isPaused = Boolean(budgets[runningTask.jobId]?.isPaused);
      if (isPaused) {
        return {
          state: "timeout" as const,
          jobId: runningTask.jobId,
          target: `/planning/${runningTask.jobId}`,
          markRead: false,
          city,
          stepText: "",
        };
      }
      const progress =
        currentJobId === runningTask.jobId ? stageProgress : null;
      const hasProgress =
        typeof progress?.step === "number" && typeof progress?.total === "number";
      return {
        state: "binding" as const,
        jobId: runningTask.jobId,
        target: `/planning/${runningTask.jobId}`,
        markRead: false,
        city,
        stepText: hasProgress ? `${progress.step}/${progress.total}` : "",
      };
    }

    // 2. Unread terminal task in tripTaskStore
    const unreadTask = Object.values(tasks)
      .filter((t) => t.notificationState === "unread")
      .sort((a, b) => b.startedAt - a.startedAt)[0];

    if (unreadTask) {
      const city = unreadTask.destination || "行程";
      if (unreadTask.status === "ready" && unreadTask.resultRecordId) {
        const isBasic = unreadTask.delivery?.outcome === "BASIC";
        return {
          state: isBasic ? ("basic" as const) : ("ready" as const),
          jobId: unreadTask.jobId,
          target: buildResultHref(unreadTask.resultRecordId, unreadTask.jobId),
          markRead: true,
          city,
          stepText: "",
        };
      }
      if (unreadTask.status === "failed" || unreadTask.status === "no_route") {
        return {
          state: "no_route" as const,
          jobId: unreadTask.jobId,
          target: `/planning/${unreadTask.jobId}`,
          markRead: true,
          city,
          stepText: "",
        };
      }
      if (unreadTask.status === "timeout") {
        return {
          state: "timeout" as const,
          jobId: unreadTask.jobId,
          target: `/planning/${unreadTask.jobId}`,
          markRead: true,
          city,
          stepText: "",
        };
      }
    }

    return null;
  }, [activeSection, tasks, budgets, currentJobId, stageProgress]);

  const handleBindChipClick = () => {
    if (!bindChip) return;
    if (bindChip.markRead) {
      acknowledgeTask(bindChip.jobId);
    }
    navigate(bindChip.target);
  };

  return (
    <header className="topbar">
      <Link className="brand" to="/" data-view="start" aria-label="云途首页">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M7.2 17.5h9.6a3.9 3.9 0 0 0 .5-7.8 5.4 5.4 0 0 0-10.3 1.2 3.3 3.3 0 0 0 .2 6.6z" />
            <circle cx="9.6" cy="14.2" r="1.1" />
            <path d="M10.7 14.2h3.6" />
          </svg>
        </span>
        <span className="brand-name">云途</span>
      </Link>

      <nav className="sections" aria-label="主导航">
        <Link
          to="/"
          data-view="start"
          aria-current={activeSection === "depart" ? "page" : undefined}
        >
          出发
        </Link>
        <Link
          to={bookHref}
          data-view="book"
          aria-current={activeSection === "book" ? "page" : undefined}
        >
          旅行册
        </Link>
        <Link
          to="/history"
          data-view="shelf"
          aria-current={activeSection === "trips" ? "page" : undefined}
        >
          我的行程
        </Link>
      </nav>

      <button
        className={`bind-chip${bindChip ? ` is-${bindChip.state}` : ""}`}
        id="bind-chip"
        type="button"
        hidden={!bindChip}
        data-state={bindChip?.state}
        onClick={handleBindChipClick}
      >
        {bindChip?.state === "binding" && (
          <>
            <i />
            {bindChip.city} · 装订中{bindChip.stepText ? ` ${bindChip.stepText}` : ""}
          </>
        )}
        {bindChip?.state === "ready" && (
          <>
            {bindChip.city} · 装订好了 · 翻开 <ArrowRightIcon />
          </>
        )}
        {bindChip?.state === "basic" && (
          <>
            {bindChip.city} · 基础版好了 · 翻开 <ArrowRightIcon />
          </>
        )}
        {bindChip?.state === "no_route" && (
          <>
            {bindChip.city} · 没能装订 · 看原因 <ArrowRightIcon />
          </>
        )}
        {bindChip?.state === "timeout" && <>{bindChip.city} · 还在装订</>}
      </button>

      <div className="account" id="account">
        <span className="user" title="开源免登录 · 本地浏览器保存行程">
          <span className="avatar" aria-hidden="true">
            开
          </span>
          开源免登录
        </span>
      </div>
    </header>
  );
}

export default SpreadTopBar;
