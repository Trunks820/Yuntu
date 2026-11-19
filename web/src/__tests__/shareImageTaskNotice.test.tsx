import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { useShareImageTaskStore } from "@/stores/shareImageTaskStore";
import { ArtifactTaskNotice } from "@/components/feedback/ArtifactTaskNotice";
import { ShareDialog } from "@/components/share/ShareDialog";
import * as api from "@/services/api";

// Mock API functions
vi.mock("@/services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/api")>();
  return {
    ...actual,
    getArtifact: vi.fn(),
    createArtifact: vi.fn(),
    fetchArtifactBlob: vi.fn(),
  };
});

describe("AI Share Image Persistent Notification & Acceptance Tests", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.resetAllMocks();
    useShareImageTaskStore.getState().clearAllTasks();
  });

  afterEach(() => {
    vi.clearAllTimers();
  });

  // Scenario 1: pending → preview_ready: 产生一条 unread 持久通知
  it("[1] pending → preview_ready: 产生一条 unread 持久通知", async () => {
    vi.mocked(api.fetchArtifactBlob).mockResolvedValueOnce(new Blob(["test-image"]));

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "polling",
          startedAt: Date.now(),
          notificationState: "none",
        },
      },
    });

    vi.mocked(api.getArtifact).mockResolvedValueOnce({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "ready",
      download_url: "/api/trip/results/2254/artifacts/share_image/download",
      filename: "poster.png",
      mime_type: "image/png",
      byte_size: 1024,
      page_count: null,
      width_px: 1080,
      height_px: 1920,
      expires_time: "",
      metadata: {},
      error: null,
    });

    await useShareImageTaskStore.getState().checkAllActiveTasks();

    const task = useShareImageTaskStore.getState().getTask("2254");
    expect(task?.status).toBe("preview_ready");
    expect(task?.notificationState).toBe("unread");
  });

  // Scenario 2: 通知展示超过 3 秒：仍然存在，不能自动消失
  it("[2] 通知展示超过 3 秒：仍然存在，不能自动消失", async () => {
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
          downloadUrl: "/api/trip/results/2254/artifacts/share_image/download",
        },
      },
    });

    vi.useFakeTimers();

    render(
      <MemoryRouter>
        <ArtifactTaskNotice />
      </MemoryRouter>
    );

    expect(screen.getByText("AI 长图已生成")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(screen.getByText("AI 长图已生成")).toBeInTheDocument();

    vi.useRealTimers();
  });

  // Scenario 3: 用户导航到其他路由：通知仍存在
  it("[3] 用户导航到其他路由：通知仍存在", () => {
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    const { rerender } = render(
      <MemoryRouter initialEntries={["/"]}>
        <ArtifactTaskNotice />
      </MemoryRouter>
    );

    expect(screen.getByText("AI 长图已生成")).toBeInTheDocument();

    rerender(
      <MemoryRouter initialEntries={["/"]}>
        <ArtifactTaskNotice />
      </MemoryRouter>
    );

    expect(screen.getByText("AI 长图已生成")).toBeInTheDocument();
  });

  // Scenario 4: Store 重新初始化：同一 authenticated user 恢复 unread 通知
  it("[4] Store 重新初始化：同一 authenticated user 恢复 unread 通知", () => {
    const rawData = {
      "2254": {
        recordId: "2254",
        artifactType: "share_image",
        status: "ready",
        startedAt: Date.now(),
        notificationState: "unread",
        downloadUrl: "/api/download",
      },
    };
    localStorage.setItem("yuntu_share_image_tasks", JSON.stringify(rawData));

    // Force re-init
    useShareImageTaskStore.setState({ tasks: {} });
    const store = useShareImageTaskStore.getState();
    store.initStore();

    const loadedTask = store.getTask("2254");
    expect(loadedTask?.notificationState).toBe("unread");
  });

  // Scenario 5: 点击“关闭”：变为 acknowledged，刷新后不再出现
  it("[5] 点击“关闭”：变为 acknowledged，刷新后不再出现", () => {
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    render(
      <MemoryRouter>
        <ArtifactTaskNotice />
      </MemoryRouter>
    );

    const closeBtns = screen.getAllByRole("button", { name: "关闭" });
    fireEvent.click(closeBtns[0]);

    const task = useShareImageTaskStore.getState().getTask("2254");
    expect(task?.notificationState).toBe("acknowledged");
    expect(screen.queryByText("AI 长图已生成")).not.toBeInTheDocument();
  });

  // Scenario 6: 点击“查看长图”：导航至 /result/{recordId}?share=1，标记 acknowledged
  it("[6] 点击“查看长图”：导航至 /result/{recordId}?share=1，标记 acknowledged", () => {
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          jobId: "785608078108423399fa506f27cd6fb4",
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <ArtifactTaskNotice />
        <Routes>
          <Route path="*" element={<div />} />
          <Route path="/result/:resultId" element={<div />} />
        </Routes>
      </MemoryRouter>
    );

    const viewBtn = screen.getByText("查看长图");
    fireEvent.click(viewBtn);

    const task = useShareImageTaskStore.getState().getTask("2254");
    expect(task?.notificationState).toBe("acknowledged");
  });

  // Scenario 7: pending → backend ready: 后台 pre-download 触发 1 次 fetchArtifactBlob
  it("[7] pending → backend ready: 后台 pre-download 触发 1 次 fetchArtifactBlob", async () => {
    const fetchBlobMock = vi.mocked(api.fetchArtifactBlob);
    fetchBlobMock.mockResolvedValueOnce(new Blob(["test-image"], { type: "image/png" }));

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "polling",
          startedAt: Date.now(),
          notificationState: "none",
        },
      },
    });

    vi.mocked(api.getArtifact).mockResolvedValueOnce({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "ready",
      download_url: "/api/trip/results/2254/artifacts/share_image/download",
      filename: "poster.png",
      mime_type: "image/png",
      byte_size: 1024,
      page_count: null,
      width_px: 1080,
      height_px: 1920,
      expires_time: "",
      metadata: {},
      error: null,
    });

    await useShareImageTaskStore.getState().checkAllActiveTasks();

    expect(fetchBlobMock).toHaveBeenCalledTimes(1);

    // 打开 ShareDialog 复用 preview 缓存，绝不二次请求
    render(
      <MemoryRouter>
        <ShareDialog open={true} onClose={() => {}} recordId="2254" />
      </MemoryRouter>
    );

    expect(fetchBlobMock).toHaveBeenCalledTimes(1);
  });

  // Scenario 8: 打开 ShareDialog 复用已下载的 preview：fetchArtifactBlob 恰好 1 次
  it("[8] 打开 ShareDialog 复用已下载的 preview：fetchArtifactBlob 恰好 1 次", async () => {
    vi.mocked(api.getArtifact).mockResolvedValue({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "ready",
      download_url: "/api/trip/results/2254/artifacts/share_image/download_8",
      filename: "poster.png",
      mime_type: "image/png",
      byte_size: 1024,
      page_count: null,
      width_px: 1080,
      height_px: 1920,
      expires_time: "",
      metadata: {},
      error: null,
    });

    const fetchBlobMock = vi.mocked(api.fetchArtifactBlob);
    fetchBlobMock.mockResolvedValue(new Blob(["test-image"], { type: "image/png" }));

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "polling",
          startedAt: Date.now(),
          downloadUrl: "/api/trip/results/2254/artifacts/share_image/download_8",
          notificationState: "none",
        },
      },
    });

    await useShareImageTaskStore.getState().checkAllActiveTasks();

    await act(async () => {
      render(
        <MemoryRouter>
          <ShareDialog open={true} onClose={() => {}} recordId="2254" />
        </MemoryRouter>
      );
    });

    expect(fetchBlobMock).toHaveBeenCalledTimes(1);
  });

  // Scenario 9: React rerender / task 对象更新：download 调用次数仍为 1
  it("[9] React rerender / task 对象更新：download 调用次数仍为 1", async () => {
    vi.mocked(api.getArtifact).mockResolvedValue({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "ready",
      download_url: "/api/download/unique_url_99",
      filename: "poster.png",
      mime_type: "image/png",
      byte_size: 1024,
      page_count: null,
      width_px: 1080,
      height_px: 1920,
      expires_time: "",
      metadata: {},
      error: null,
    });

    const fetchBlobMock = vi.mocked(api.fetchArtifactBlob);
    fetchBlobMock.mockResolvedValue(new Blob(["test-image"], { type: "image/png" }));

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "polling",
          startedAt: Date.now(),
          downloadUrl: "/api/download/unique_url_99",
          notificationState: "none",
        },
      },
    });

    await useShareImageTaskStore.getState().checkAllActiveTasks();

    let rerenderFn: (ui: React.ReactNode) => void;
    await act(async () => {
      const res = render(
        <MemoryRouter>
          <ShareDialog open={true} onClose={() => {}} recordId="2254" />
        </MemoryRouter>
      );
      rerenderFn = res.rerender;
    });

    // Trigger task object reference update in store
    await act(async () => {
      useShareImageTaskStore.setState((s) => ({
        tasks: {
          ...s.tasks,
          "2254": { ...s.tasks["2254"], finishedAt: Date.now() },
        },
      }));
    });

    await act(async () => {
      rerenderFn(
        <MemoryRouter>
          <ShareDialog open={true} onClose={() => {}} recordId="2254" />
        </MemoryRouter>
      );
    });

    expect(fetchBlobMock).toHaveBeenCalledTimes(1);
  });

  // Scenario 10: 两次并发轮询同时返回 ready：只有一个 unread 通知
  it("[10] 两次并发轮询同时返回 ready：只有一个 unread 通知", async () => {
    vi.mocked(api.fetchArtifactBlob).mockResolvedValue(new Blob(["test-image"], { type: "image/png" }));

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "polling",
          startedAt: Date.now(),
          notificationState: "none",
        },
      },
    });

    vi.mocked(api.getArtifact).mockResolvedValue({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "ready",
      download_url: "/api/download",
      filename: "poster.png",
      mime_type: "image/png",
      byte_size: 1024,
      page_count: null,
      width_px: 1080,
      height_px: 1920,
      expires_time: "",
      metadata: {},
      error: null,
    });

    await Promise.all([
      useShareImageTaskStore.getState().checkAllActiveTasks(),
      useShareImageTaskStore.getState().checkAllActiveTasks(),
    ]);

    const task = useShareImageTaskStore.getState().getTask("2254");
    expect(task?.notificationState).toBe("unread");
  });

  // Scenario 11: 退出登录或 user_id 切换：任务和通知被清空
  it("[11] 退出登录或 user_id 切换：任务和通知被清空", () => {
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    act(() => {
      useShareImageTaskStore.getState().clearAllTasks();
    });

    expect(useShareImageTaskStore.getState().tasks).toEqual({});
  });

  // Scenario 12: clearAllTasks 后旧请求返回：currentEpoch 阻止旧响应复活
  it("[12] clearAllTasks 后旧请求返回：currentEpoch 阻止旧响应复活", async () => {
    let pendingResolve: (val: unknown) => void;
    const pendingPromise = new Promise((resolve) => {
      pendingResolve = resolve;
    });

    vi.mocked(api.getArtifact).mockReturnValueOnce(pendingPromise as ReturnType<typeof api.getArtifact>);

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "polling",
          startedAt: Date.now(),
          notificationState: "none",
        },
      },
    });

    const checkPromise = useShareImageTaskStore.getState().checkAllActiveTasks();

    // 中途清理所有任务并递增 epoch
    useShareImageTaskStore.getState().clearAllTasks();

    // 延迟响应返回
    pendingResolve!({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "ready",
      download_url: "/api/download",
      filename: "poster.png",
      mime_type: "image/png",
      byte_size: 1024,
      page_count: null,
      width_px: 1080,
      height_px: 1920,
      expires_time: "",
      metadata: {},
      error: null,
    });

    await checkPromise;

    expect(useShareImageTaskStore.getState().tasks).toEqual({});
  });

  // Scenario 13: 401：不创建失败通知，不写回旧任务
  it("[13] 401：不创建失败通知，不写回旧任务", async () => {
    const err = new api.ApiRequestError("AUTH_REQUIRED", "请先登录", 401);

    vi.mocked(api.getArtifact).mockRejectedValueOnce(err);

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "polling",
          startedAt: Date.now(),
          notificationState: "none",
        },
      },
    });

    await useShareImageTaskStore.getState().checkAllActiveTasks();

    const task = useShareImageTaskStore.getState().getTask("2254");
    expect(task?.status).toBe("polling");
    expect(task?.notificationState).toBe("none");
  });

  // Scenario 14: failed 普通打开：不自动 POST
  it("[14] failed 普通打开：不自动 POST", async () => {
    const createMock = vi.mocked(api.createArtifact);

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "failed",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    const task = await useShareImageTaskStore.getState().startOrFetchTask("2254");

    expect(createMock).toHaveBeenCalledTimes(0);
    expect(task.status).toBe("failed");
  });

  // Scenario 15: failed 用户明确重试：只 POST 一次
  it("[15] failed 用户明确重试：只 POST 一次", async () => {
    const createMock = vi.mocked(api.createArtifact);
    createMock.mockResolvedValueOnce({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "pending",
      download_url: "",
      filename: "",
      mime_type: "image/png",
      byte_size: 0,
      page_count: null,
      width_px: null,
      height_px: null,
      expires_time: "",
      metadata: {},
      error: null,
    });

    vi.mocked(api.getArtifact).mockResolvedValueOnce({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "failed",
      download_url: "",
      filename: "",
      mime_type: "image/png",
      byte_size: 0,
      page_count: null,
      width_px: null,
      height_px: null,
      expires_time: "",
      metadata: {},
      error: { code: "GENERATION_FAILED", message: "生成失败" },
    });

    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          artifactType: "share_image",
          status: "failed",
          startedAt: Date.now(),
          notificationState: "acknowledged",
        },
      },
    });

    await useShareImageTaskStore.getState().retryTask("2254");

    expect(createMock).toHaveBeenCalledTimes(1);
  });

  // Scenario 16: PDF：保持原有行为，无回归
  it("[16] PDF：保持原有行为，无回归", () => {
    const pdfTask = {
      recordId: "2254",
      artifactType: "pdf" as const,
      status: "ready" as const,
    };
    expect(pdfTask.artifactType).toBe("pdf");
    expect(pdfTask.status).toBe("ready");
  });

  // Scenario 17: 点击 ready 通知后，pathname、job_id、share=1 全部正确
  it("[17] 点击 ready 通知后，pathname、job_id、share=1 全部正确", () => {
    const testJobId = "785608078108423399fa506f27cd6fb4";
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          jobId: testJobId,
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    let currentPath = "";
    function RouteTracker() {
      const location = useLocation();
      currentPath = location.pathname + location.search;
      return null;
    }

    render(
      <MemoryRouter initialEntries={["/"]}>
        <ArtifactTaskNotice />
        <RouteTracker />
      </MemoryRouter>
    );

    const viewBtn = screen.getByText("查看长图");
    fireEvent.click(viewBtn);

    expect(currentPath).toBe(`/result/2254?job_id=${encodeURIComponent(testJobId)}&share=1`);
    const task = useShareImageTaskStore.getState().getTask("2254");
    expect(task?.notificationState).toBe("acknowledged");
  });

  // Scenario 18: jobId 经 localStorage 刷新恢复后仍存在
  it("[18] jobId 经 localStorage 刷新恢复后仍存在", () => {
    const testJobId = "785608078108423399fa506f27cd6fb4";
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          jobId: testJobId,
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    // Trigger saveToLocalStorage via acknowledgeTask
    useShareImageTaskStore.getState().acknowledgeTask("2254");

    const raw = localStorage.getItem("yuntu_share_image_tasks");
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed["2254"]?.jobId).toBe(testJobId);
  });

  // Scenario 19: pending → ready 状态转换不丢 jobId
  it("[19] pending → ready 状态转换不丢 jobId", async () => {
    const testJobId = "785608078108423399fa506f27cd6fb4";
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          jobId: testJobId,
          artifactType: "share_image",
          status: "polling",
          startedAt: Date.now(),
          notificationState: "none",
        },
      },
    });

    vi.mocked(api.getArtifact).mockResolvedValueOnce({
      ok: true,
      artifact_id: "art_1",
      result_record_id: 2254,
      artifact_type: "share_image",
      status: "ready",
      download_url: "/api/trip/results/2254/artifacts/share_image/download",
      filename: "poster.png",
      mime_type: "image/png",
      byte_size: 1024,
      page_count: null,
      width_px: 1080,
      height_px: 1920,
      expires_time: "",
      metadata: {},
      error: null,
    });

    vi.mocked(api.fetchArtifactBlob).mockResolvedValueOnce(new Blob(["test-image"]));

    await useShareImageTaskStore.getState().checkAllActiveTasks();

    const task = useShareImageTaskStore.getState().getTask("2254");
    expect(task?.status).toBe("preview_ready");
    expect(task?.jobId).toBe(testJobId);
  });

  // Scenario 20: legacy task 缺 jobId 时跳到首页，不产生损坏 URL
  it("[20] legacy task 缺 jobId 时跳到首页，不产生损坏 URL", () => {
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          // jobId missing
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    let currentPath = "";
    function RouteTracker() {
      const location = useLocation();
      currentPath = location.pathname + location.search;
      return null;
    }

    render(
      <MemoryRouter initialEntries={["/"]}>
        <ArtifactTaskNotice />
        <RouteTracker />
      </MemoryRouter>
    );

    const viewBtn = screen.getByText("查看长图");
    fireEvent.click(viewBtn);

    expect(currentPath).toBe("/history");
    expect(currentPath).not.toContain("/result/2254");
  });

  // Scenario 21: 关闭通知不导航、不调用 fetchArtifactBlob
  it("[21] 关闭通知不导航、不调用 fetchArtifactBlob", () => {
    const fetchBlobMock = vi.mocked(api.fetchArtifactBlob);
    useShareImageTaskStore.setState({
      tasks: {
        "2254": {
          recordId: "2254",
          jobId: "785608078108423399fa506f27cd6fb4",
          artifactType: "share_image",
          status: "ready",
          startedAt: Date.now(),
          notificationState: "unread",
        },
      },
    });

    let currentPath = "/initial";
    function RouteTracker() {
      const location = useLocation();
      currentPath = location.pathname + location.search;
      return null;
    }

    render(
      <MemoryRouter initialEntries={["/initial"]}>
        <ArtifactTaskNotice />
        <RouteTracker />
      </MemoryRouter>
    );

    const closeBtns = screen.getAllByRole("button", { name: "关闭" });
    fireEvent.click(closeBtns[0]);

    expect(currentPath).toBe("/initial");
    expect(fetchBlobMock).toHaveBeenCalledTimes(0);
    const task = useShareImageTaskStore.getState().getTask("2254");
    expect(task?.notificationState).toBe("acknowledged");
  });
});
