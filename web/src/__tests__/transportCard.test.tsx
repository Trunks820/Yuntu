import { fireEvent, render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { TransportCard } from "@/components/detail/TransportCard";
import type { TripTransport } from "@/types/trip";

describe("TransportCard Component Tests (Equal Weight Spec §2.2)", () => {
  it("[PR1-1] transport 为 null 时，组件返回 null，零渲染零报错", () => {
    const { container } = render(<TransportCard data={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("[PR1-2] transport 为 undefined 时，组件返回 null", () => {
    const { container } = render(<TransportCard data={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it("[PR1-3] 单 mode (train) 结论卡：包含地名对排、价格范围原样展示及车次参考", () => {
    const mockTransport: TripTransport = {
      from_city: "北京",
      to_city: "上海",
      query_date: "2026-08-15",
      source: "realtime",
      modes: [
        {
          mode: "train",
          min_duration_minutes: 270,
          price_range: "¥626-661",
          price_source: "realtime",
          daily_count: 52,
          data_source: "realtime",
          availability_status: "available_at_query",
          availability_checked_at: "2026-08-02T10:00:00Z",
          options: [
            {
              type: "train",
              no: "G3",
              departure_time: "09:00",
              arrival_time: "13:40",
              duration_minutes: 280,
              price: "¥626",
              departure_station: "北京南",
              arrival_station: "上海虹桥",
              airline: null,
            },
          ],
        },
      ],
    };

    render(<TransportCard data={mockTransport} />);

    expect(screen.getByText("去程大交通推荐")).toBeInTheDocument();
    expect(screen.getByText("北京")).toBeInTheDocument();
    expect(screen.getByText("上海")).toBeInTheDocument();
    expect(screen.getByText("高铁")).toBeInTheDocument();
    expect(screen.getByText("¥626-661")).toBeInTheDocument();
    expect(screen.getByText("每日约 52 班")).toBeInTheDocument();
    expect(screen.getByText("最快 4小时30分")).toBeInTheDocument();
  });

  it("[PR1-4] 单 mode (flight) 合法独立渲染", () => {
    const mockTransport: TripTransport = {
      from_city: "上海",
      to_city: "三亚",
      query_date: "2026-09-01",
      source: "static_fallback",
      modes: [
        {
          mode: "flight",
          min_duration_minutes: 210,
          price_range: "¥980(参考价)",
          price_source: "static_reference",
          daily_count: 15,
          data_source: "static_fallback",
          availability_status: "unknown",
          availability_checked_at: null,
          options: [],
        },
      ],
    };

    render(<TransportCard data={mockTransport} />);

    expect(screen.getByText("飞机")).toBeInTheDocument();
    expect(screen.getByText("¥980(参考价)")).toBeInTheDocument();
    expect(screen.getByText("静态估算，班次与价格以购票平台为准")).toBeInTheDocument();
  });

  it("[PR1-5] 双 mode (train + flight) 等权同级渲染，无降权", () => {
    const mockTransport: TripTransport = {
      from_city: "北京",
      to_city: "广州",
      query_date: "2026-08-20",
      source: "realtime",
      modes: [
        {
          mode: "train",
          min_duration_minutes: 480,
          price_range: "¥862",
          price_source: "realtime",
          daily_count: 12,
          data_source: "realtime",
          availability_status: "available_at_query",
          availability_checked_at: null,
          options: [],
        },
        {
          mode: "flight",
          min_duration_minutes: 190,
          price_range: "¥1200(参考价)",
          price_source: "static_reference",
          daily_count: 30,
          data_source: "static_fallback",
          availability_status: "unknown",
          availability_checked_at: null,
          options: [],
        },
      ],
    };

    render(<TransportCard data={mockTransport} />);

    expect(screen.getByText("高铁")).toBeInTheDocument();
    expect(screen.getByText("飞机")).toBeInTheDocument();
    expect(screen.getByText("¥862")).toBeInTheDocument();
    expect(screen.getByText("¥1200(参考价)")).toBeInTheDocument();
  });

  it("[PR1-6] 售罄状态 sold_out_at_query 渲染琥珀色提醒", () => {
    const mockTransport: TripTransport = {
      from_city: "成都",
      to_city: "重庆",
      query_date: "2026-08-10",
      source: "realtime",
      modes: [
        {
          mode: "train",
          min_duration_minutes: 90,
          price_range: "¥154",
          price_source: "realtime",
          daily_count: 20,
          data_source: "realtime",
          availability_status: "sold_out_at_query",
          availability_checked_at: null,
          options: [],
        },
      ],
    };

    render(<TransportCard data={mockTransport} />);

    expect(
      screen.getByText("查询时段车票已售罄，请以购票平台实时余票为准"),
    ).toBeInTheDocument();
  });

  it("使用 lucide 线描图标，不再渲染 🚄 或 ✈️ 表情符号", () => {
    const mockTransport: TripTransport = {
      from_city: "北京",
      to_city: "广州",
      query_date: "2026-08-20",
      source: "realtime",
      modes: [
        {
          mode: "train",
          min_duration_minutes: 480,
          price_range: "¥862",
          price_source: "realtime",
          daily_count: 12,
          data_source: "realtime",
          availability_status: "available_at_query",
          availability_checked_at: null,
          options: [
            {
              type: "train",
              no: "G79",
              departure_time: "10:00",
              arrival_time: "18:00",
              duration_minutes: 480,
              price: "¥862",
              departure_station: "北京西",
              arrival_station: "广州南",
              airline: null,
            },
          ],
        },
        {
          mode: "flight",
          min_duration_minutes: 190,
          price_range: "¥1200",
          price_source: "realtime",
          daily_count: 30,
          data_source: "realtime",
          availability_status: "available_at_query",
          availability_checked_at: null,
          options: [
            {
              type: "flight",
              no: "CA1301",
              departure_time: "08:30",
              arrival_time: "11:40",
              duration_minutes: 190,
              price: "¥1200",
              departure_station: "首都机场",
              arrival_station: "白云机场",
              airline: "国航",
            },
          ],
        },
      ],
    };

    const { container } = render(<TransportCard data={mockTransport} />);
    fireEvent.click(screen.getByRole("button", { name: /车次参考/ }));
    fireEvent.click(screen.getByRole("button", { name: /航班参考/ }));

    expect(container.textContent).not.toContain("🚄");
    expect(container.textContent).not.toContain("✈");
    expect(container.querySelectorAll("svg.lucide-train-front").length).toBeGreaterThanOrEqual(3);
    expect(container.querySelectorAll("svg.lucide-plane").length).toBeGreaterThanOrEqual(3);
  });
});
