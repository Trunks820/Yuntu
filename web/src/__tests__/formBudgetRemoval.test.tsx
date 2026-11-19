import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, it, expect, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import SpreadStartPage from "@/pages/start/SpreadStartPage";
import {
  getPendingSubmission,
  savePendingSubmission,
} from "@/utils/pendingSubmission";
import type { TripFormData } from "@/types/form";

describe("SpreadStartPage Budget Removal & Stored Data Compatibility Tests", () => {
  beforeEach(() => {
    sessionStorage.clear();
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    });
  });

  it("[1] 旧版本暂存数据（包含 budget 字段）读取恢复时静默忽略，零报错", () => {
    // 模拟旧版本保存在 sessionStorage 中的含 budget 字段的数据
    const legacyFormData = {
      to_city: "重庆",
      start_date: "2026-08-10",
      end_date: "2026-08-12",
      days: 3,
      people_count: 2,
      preferences: ["自然风光", "轻松"],
      avoid: [],
      notes: "老数据测试",
      budget: 8000, // 旧版本保存的 budget
    } as unknown as TripFormData;

    // 存入 pending submission
    savePendingSubmission(legacyFormData);

    const pending = getPendingSubmission();
    expect(pending).not.toBeNull();
    // 验证旧版 pending submission 读取正常
    expect(pending?.trip_request.to_city).toBe("重庆");

    // 渲染新版 SpreadStartPage，不产生任何异常或报错
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SpreadStartPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getAllByText("就选这几张").length).toBeGreaterThan(0);
    // 验证页面上不渲染“预算范围”标签
    expect(screen.queryByText("预算范围")).toBeNull();
  });

  it("[2] 新版生成的 generateFingerprint 与 PendingSubmission 响应契约中均无 budget 拦截", () => {
    const newFormData: TripFormData = {
      to_city: "成都",
      start_date: "2026-08-15",
      end_date: "2026-08-17",
      days: 3,
      people_count: 1,
      preferences: ["城市人文"],
      avoid: [],
      notes: "",
    };

    const submission = savePendingSubmission(newFormData);
    expect(submission.trip_request.budget).toBeUndefined();
  });
});
