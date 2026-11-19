import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { useTripTaskStore } from "@/stores/tripTaskStore";
import { useShareImageTaskStore } from "@/stores/shareImageTaskStore";
import { TripTaskNotice } from "@/components/feedback/TripTaskNotice";
import { ArtifactTaskNotice } from "@/components/feedback/ArtifactTaskNotice";
import * as api from "@/services/api";

vi.mock("@/services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/api")>();
  return {
    ...actual,
    pollJobStatus: vi.fn(),
  };
});

describe("Trip Generation Persistent Task Notification Tests (P1)", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.resetAllMocks();
    useTripTaskStore.getState().clearAllTasks();
    useShareImageTaskStore.getState().clearAllTasks();
  });

  afterEach(() => {
    vi.clearAllTimers();
  });

  it("[P1-1] pending 攻略任务完成 -> 产生 unread 通知并能跳转", async () => {
    useTripTaskStore.setState({
      tasks: {
        job_100: {
          accountId: "101",
          jobId: "job_100",
          destination: "东京",
          startedAt: Date.now(),
          status: "pending",
          notificationState: "none",
        },
      },
    });

    vi.mocked(api.pollJobStatus).mockResolvedValueOnce({
      ok: true,
      job_id: "job_100",
      status: "COMPLETED",
      stage_progress: { code: "FINALIZING", step: 4, total: 4 },
      result_record_id: "2254",
      error: null,
    });

    await useTripTaskStore.getState().checkAllActiveTasks();

    const task = useTripTaskStore.getState().getTask("job_100");
    expect(task?.status).toBe("ready");
    expect(task?.notificationState).toBe("unread");
    expect(String(task?.resultRecordId)).toBe("2254");

    let currentPath = "";
    function LocationTracker() {
      const loc = useLocation();
      currentPath = loc.pathname + loc.search;
      return null;
    }

    render(
      <MemoryRouter initialEntries={["/"]}>
        <LocationTracker />
        <TripTaskNotice />
      </MemoryRouter>
    );

    expect(screen.getByText("旅行攻略已生成")).toBeInTheDocument();
    expect(screen.getByText("为你准备好了 东京 的行程攻略")).toBeInTheDocument();

    const btn = screen.getByText("查看结果");
    fireEvent.click(btn);

    expect(currentPath).toBe("/result/2254?job_id=job_100");
    expect(useTripTaskStore.getState().getTask("job_100")?.notificationState).toBe("acknowledged");
  });

  it("[P1-2] 失败任务显示‘旅行攻略生成失败’，重新规划重定向至首页", async () => {
    useTripTaskStore.setState({
      tasks: {
        job_101: {
          accountId: "101",
          jobId: "job_101",
          destination: "巴黎",
          startedAt: Date.now(),
          status: "failed",
          notificationState: "unread",
        },
      },
    });

    let currentPath = "";
    function LocationTracker() {
      const loc = useLocation();
      currentPath = loc.pathname + loc.search;
      return null;
    }

    render(
      <MemoryRouter initialEntries={["/"]}>
        <LocationTracker />
        <TripTaskNotice />
      </MemoryRouter>
    );

    expect(screen.getByText("旅行攻略生成失败")).toBeInTheDocument();

    const btn = screen.getByText("重新规划");
    fireEvent.click(btn);

    expect(currentPath).toBe("/");
    expect(useTripTaskStore.getState().getTask("job_101")?.notificationState).toBe("acknowledged");
  });

  it("[P1-4] 攻略任务与长图任务同时存在时，互不干扰、独立渲染", () => {
    useTripTaskStore.setState({
      tasks: {
        job_999: {
          accountId: "101",
          jobId: "job_999",
          destination: "首尔",
          startedAt: Date.now(),
          status: "ready",
          resultRecordId: 3000,
          notificationState: "unread",
        },
      },
    });

    useShareImageTaskStore.setState({
      tasks: {
        "8000": {
          recordId: "8000",
          artifactType: "share_image",
          status: "preview_ready",
          startedAt: Date.now(),
          downloadUrl: "/api/download",
          notificationState: "unread",
        },
      },
    });

    render(
      <MemoryRouter>
        <TripTaskNotice />
        <ArtifactTaskNotice />
      </MemoryRouter>
    );

    expect(screen.getByText("旅行攻略已生成")).toBeInTheDocument();
    expect(screen.getByText("AI 长图已生成")).toBeInTheDocument();
  });
});
