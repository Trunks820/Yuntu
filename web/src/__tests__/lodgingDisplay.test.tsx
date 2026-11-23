import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AccommodationTimelineNode } from "@/components/detail/AccommodationCard";
import { Icon } from "@/components/mobile/mobileUi";
import { MobileLodgingRow } from "@/pages/mobile/MobileLodgingRow";
import { MobilePlanView } from "@/pages/mobile/MobilePlanView";
import type {
  AccommodationInfo,
  LodgingDisplay,
  LodgingDisplayGroup,
  TripPlan,
  TripResult,
} from "@/types/trip";

function textOf(container: HTMLElement): string {
  return container.textContent ?? "";
}

const TIER_GROUPS: LodgingDisplayGroup[] = [
  {
    kind: "economy",
    label: "经济",
    hotels: ["如家酒店·neo", "庭泊·别院酒店"],
    nightly_low_cny: 90,
    nightly_high_cny: 110,
  },
  {
    kind: "mid",
    label: "中档",
    hotels: ["银河大酒店", "宾顿朗逸酒店"],
    nightly_low_cny: 270,
    nightly_high_cny: 540,
  },
  {
    kind: "high",
    label: "高档",
    hotels: ["重庆国贸格兰大酒店", "君亭设计酒店"],
    nightly_low_cny: 690,
    nightly_high_cny: 830,
  },
];

const HOLIDAY_TEXT =
  "你的行程赶上国庆假期，热门片区假期房价常比平时高，建议尽早订房、预算多留一些。";

function tiersDisplay(overrides: Partial<LodgingDisplay> = {}): LodgingDisplay {
  return {
    layout: "tiers",
    area_label: "解放碑一带",
    caption: "",
    groups: TIER_GROUPS,
    round_trip_minutes_low: null,
    round_trip_minutes_high: null,
    notices: [{ kind: "holiday", text: HOLIDAY_TEXT }],
    ...overrides,
  };
}

function recommendedAcc(
  display?: LodgingDisplay | null,
  extras: Partial<AccommodationInfo> = {},
): AccommodationInfo {
  return {
    name: "解放碑片区",
    latitude: 29.557,
    longitude: 106.577,
    source: "auto_recommended",
    reason: "离第1、2、3天的点都近，每天来回约35–74分钟",
    display,
    ...extras,
  };
}

function expandHotels() {
  fireEvent.click(screen.getByRole("button", { name: /附近酒店/ }));
}

function readLodgingRows(container: HTMLElement) {
  return Array.from(container.querySelectorAll(".lodging-row")).map((row) => ({
    label: row.querySelector("b")?.textContent ?? "",
    price: row.querySelector(".p")?.textContent ?? null,
    hotels: row.querySelector(".h")?.textContent ?? "",
  }));
}

describe("手机端住宿明细 MobileLodgingRow", () => {
  it("三档默认收起，点开按一档一行显示档名、价格、店名、节假日提醒和小字", () => {
    const { container } = render(
      <MobileLodgingRow accommodation={recommendedAcc(tiersDisplay())} />,
    );

    expect(textOf(container)).toContain("住宿参考：解放碑一带");
    const toggle = screen.getByRole("button", { name: /附近酒店/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(container.querySelectorAll(".lodging-row")).toHaveLength(0);
    expect(textOf(container)).not.toContain("如家酒店·neo");
    expect(textOf(container)).not.toContain(HOLIDAY_TEXT);
    expect(textOf(container)).not.toContain("参考价，实际以预订时为准");

    expandHotels();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(container.querySelector(".lodging-detail.lodging-rows")).not.toBeNull();
    expect(readLodgingRows(container)).toEqual([
      {
        label: "经济",
        price: "约90–110元/晚",
        hotels: "如家酒店·neo、庭泊·别院酒店",
      },
      {
        label: "中档",
        price: "约270–540元/晚",
        hotels: "银河大酒店、宾顿朗逸酒店",
      },
      {
        label: "高档",
        price: "约690–830元/晚",
        hotels: "重庆国贸格兰大酒店、君亭设计酒店",
      },
    ]);
    expect(textOf(container)).toContain(HOLIDAY_TEXT);
    expect(textOf(container)).toContain("参考价，实际以预订时为准");
  });

  it("某档价格为空时不写价，其余档照常，仍有小字", () => {
    const groups: LodgingDisplayGroup[] = [
      {
        kind: "economy",
        label: "经济",
        hotels: ["如家酒店·neo"],
        nightly_low_cny: null,
        nightly_high_cny: null,
      },
      TIER_GROUPS[1],
      TIER_GROUPS[2],
    ];
    const { container } = render(
      <MobileLodgingRow
        accommodation={recommendedAcc(tiersDisplay({ groups }))}
      />,
    );
    expandHotels();
    const rows = readLodgingRows(container);
    expect(rows[0]).toEqual({
      label: "经济",
      price: "",
      hotels: "如家酒店·neo",
    });
    expect(rows[1]).toEqual({
      label: "中档",
      price: "约270–540元/晚",
      hotels: "银河大酒店、宾顿朗逸酒店",
    });
    expect(textOf(container)).toContain("参考价，实际以预订时为准");
  });

  it("民宿为主点开先写那句，再写民宿客栈行", () => {
    const { container } = render(
      <MobileLodgingRow
        accommodation={recommendedAcc(
          {
            layout: "homestay",
            area_label: "前寺一带",
            caption: "这一带以民宿客栈为主",
            groups: [
              {
                kind: "homestay",
                label: "民宿客栈",
                hotels: ["海印客栈", "短姑圣迹民宿", "百步沙驿舍"],
                nightly_low_cny: 280,
                nightly_high_cny: 560,
              },
            ],
            round_trip_minutes_low: null,
            round_trip_minutes_high: null,
            notices: [],
          },
          {
            name: "普陀山前寺一带",
            reason: "离主要景点都近",
          },
        )}
      />,
    );
    expect(textOf(container)).not.toContain("这一带以民宿客栈为主");
    expandHotels();
    expect(textOf(container)).toContain("这一带以民宿客栈为主");
    expect(readLodgingRows(container)).toEqual([
      {
        label: "民宿客栈",
        price: "约280–560元/晚",
        hotels: "海印客栈、短姑圣迹民宿、百步沙驿舍",
      },
    ]);
  });

  it("只有店名不写价，也没有参考价小字", () => {
    const { container } = render(
      <MobileLodgingRow
        accommodation={recommendedAcc(
          tiersDisplay({
            layout: "brands",
            caption: "",
            groups: [
              {
                kind: "economy",
                label: "经济",
                hotels: ["如家酒店·neo", "庭泊·别院酒店"],
                nightly_low_cny: null,
                nightly_high_cny: null,
              },
            ],
            notices: [],
          }),
        )}
      />,
    );
    expandHotels();
    expect(readLodgingRows(container)).toEqual([
      {
        label: "经济",
        price: null,
        hotels: "如家酒店·neo、庭泊·别院酒店",
      },
    ]);
    expect(textOf(container)).not.toContain("元/晚");
    expect(textOf(container)).not.toContain("参考价，实际以预订时为准");
  });

  it("groups 为空只有节假日提醒时直接显示、没有开关", () => {
    render(
      <MobileLodgingRow
        accommodation={recommendedAcc(
          tiersDisplay({
            layout: "area_only",
            groups: [],
            notices: [{ kind: "holiday", text: HOLIDAY_TEXT }],
          }),
        )}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /附近酒店/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(HOLIDAY_TEXT)).toBeInTheDocument();
    expect(
      screen.queryByText("参考价，实际以预订时为准"),
    ).not.toBeInTheDocument();
  });

  it("用户住处写来回分钟，两数相同写单值，都为空则只写出发", () => {
    const userBase: LodgingDisplay = {
      layout: "user_lodging",
      area_label: "解放碑观景酒店",
      caption: "",
      groups: [],
      round_trip_minutes_low: 20,
      round_trip_minutes_high: 45,
      notices: [],
    };
    const { container, rerender } = render(
      <MobileLodgingRow
        accommodation={{
          name: "解放碑观景酒店",
          latitude: 29.55,
          longitude: 106.57,
          source: "user_specified",
          reason: null,
          display: userBase,
        }}
      />,
    );
    expect(textOf(container)).toContain(
      "住宿：从你住的解放碑观景酒店出发，每天来回约20–45分钟",
    );
    expect(
      screen.queryByRole("button", { name: /附近酒店/ }),
    ).not.toBeInTheDocument();

    rerender(
      <MobileLodgingRow
        accommodation={{
          name: "解放碑观景酒店",
          latitude: 29.55,
          longitude: 106.57,
          source: "user_specified",
          reason: null,
          display: {
            ...userBase,
            round_trip_minutes_low: 30,
            round_trip_minutes_high: 30,
          },
        }}
      />,
    );
    expect(textOf(container)).toContain(
      "住宿：从你住的解放碑观景酒店出发，每天来回约30分钟",
    );

    rerender(
      <MobileLodgingRow
        accommodation={{
          name: "解放碑观景酒店",
          latitude: 29.55,
          longitude: 106.57,
          source: "user_specified",
          reason: null,
          display: {
            ...userBase,
            round_trip_minutes_low: null,
            round_trip_minutes_high: null,
          },
        }}
      />,
    );
    expect(textOf(container)).toContain("住宿：从你住的解放碑观景酒店出发");
    expect(textOf(container)).not.toContain("每天来回");
  });

  it("没找到住处的提醒在收起状态下也看得到，且在住宿行上面", () => {
    const unmatched = "没找到你填的住处「某某酒店」，按行程推荐住解放碑一带";
    const { container } = render(
      <MobileLodgingRow
        accommodation={recommendedAcc(
          tiersDisplay({
            notices: [
              { kind: "user_lodging_unmatched", text: unmatched },
              { kind: "holiday", text: HOLIDAY_TEXT },
            ],
          }),
        )}
      />,
    );
    expect(screen.getByText(unmatched)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /附近酒店/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText(HOLIDAY_TEXT)).not.toBeInTheDocument();
    expect(screen.queryByText(/约90–110元\/晚/)).not.toBeInTheDocument();
    const text = container.textContent ?? "";
    expect(text.indexOf(unmatched)).toBeGreaterThanOrEqual(0);
    expect(text.indexOf(unmatched)).toBeLessThan(text.indexOf("住宿参考"));
  });

  it("没有明细时输出和改前一致", () => {
    const acc = recommendedAcc(undefined);
    delete (acc as { display?: LodgingDisplay }).display;
    const expected = render(
      <div className="manifest-row">
        <Icon name="info" />
        <span>
          <b>住宿参考</b>：{acc.name}
          {acc.reason ? `（${acc.reason}）` : ""}
        </span>
      </div>,
    );
    const actual = render(<MobileLodgingRow accommodation={acc} />);
    expect(actual.container.innerHTML).toBe(expected.container.innerHTML);

    const accNull = recommendedAcc(null);
    const actualNull = render(<MobileLodgingRow accommodation={accNull} />);
    expect(actualNull.container.innerHTML).toBe(expected.container.innerHTML);
  });

  it("两数相同的价格写成约N元/晚；店名原样不截断不排序", () => {
    const { container } = render(
      <MobileLodgingRow
        accommodation={recommendedAcc(
          tiersDisplay({
            groups: [
              {
                kind: "economy",
                label: "经济",
                hotels: ["庭泊·别院酒店（超长店名也不截断）", "如家酒店·neo"],
                nightly_low_cny: 90,
                nightly_high_cny: 90,
              },
            ],
            notices: [],
          }),
        )}
      />,
    );
    expandHotels();
    expect(readLodgingRows(container)).toEqual([
      {
        label: "经济",
        price: "约90元/晚",
        hotels: "庭泊·别院酒店（超长店名也不截断）、如家酒店·neo",
      },
    ]);
  });

  it("BFF 去掉 null 键后（价格、来回分钟缺省）照常显示", () => {
    const { container } = render(
      <MobileLodgingRow
        accommodation={recommendedAcc({
          layout: "brands",
          area_label: "解放碑一带",
          caption: "",
          groups: [{ kind: "economy", label: "经济", hotels: ["如家酒店·neo"] }],
          notices: [],
        })}
      />,
    );
    expandHotels();
    expect(readLodgingRows(container)).toEqual([
      {
        label: "经济",
        price: null,
        hotels: "如家酒店·neo",
      },
    ]);
    expect(textOf(container)).not.toContain("元/晚");
    expect(textOf(container)).not.toContain("undefined");
  });
});

describe("手机旅行册接入 MobilePlanView", () => {
  it("旧记录仍渲染原来的住宿参考行", () => {
    const plan = createPlan(
      recommendedAcc(undefined, {
        name: "古城南门客栈",
        reason: "步行可达古城",
      }),
    );
    delete (plan.accommodation as { display?: LodgingDisplay }).display;
    const { container } = renderPlanView(plan);
    expect(textOf(container)).toContain(
      "住宿参考：古城南门客栈（步行可达古城）",
    );
    expect(
      screen.queryByRole("button", { name: /附近酒店/ }),
    ).not.toBeInTheDocument();
  });
});

describe("PC 住宿卡片 AccommodationTimelineNode", () => {
  it("推荐住处不再出现地理位置优越，有理由时理由照常", () => {
    render(
      <AccommodationTimelineNode
        day={1}
        accommodation={recommendedAcc(null, {
          name: "解放碑/洪崖洞商圈",
          reason: "步行可达十八梯与洪崖洞",
        })}
      />,
    );
    expect(screen.queryByText(/地理位置优越/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/步行或公共交通可便捷直达绝大多数核心景点/),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("💡 推荐理由：步行可达十八梯与洪崖洞"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("建议住在 解放碑/洪崖洞商圈 附近"),
    ).toBeInTheDocument();
  });

  it("推荐住处没理由时没有说明区，卡片只剩标题", () => {
    const { container } = render(
      <AccommodationTimelineNode
        day={1}
        accommodation={recommendedAcc(null, {
          name: "解放碑/洪崖洞商圈",
          reason: null,
        })}
      />,
    );
    expect(screen.queryByText(/地理位置优越/)).not.toBeInTheDocument();
    expect(screen.queryByText(/推荐理由/)).not.toBeInTheDocument();
    expect(container.querySelector(".mt-3.border-t")).toBeNull();
    expect(
      screen.getByText("建议住在 解放碑/洪崖洞商圈 附近"),
    ).toBeInTheDocument();
  });

  it("用户住处那句还在", () => {
    render(
      <AccommodationTimelineNode
        day={1}
        accommodation={{
          name: "解放碑观景酒店",
          latitude: 29.55,
          longitude: 106.57,
          source: "user_specified",
          reason: null,
        }}
      />,
    );
    expect(
      screen.getByText(
        "本行程路线已围绕你的入住点【解放碑观景酒店】进行交通与路线最优化排布。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/地理位置优越/)).not.toBeInTheDocument();
  });
});

function createPlan(accommodation: AccommodationInfo): TripPlan {
  return {
    plan_id: "plan_1",
    title: "重庆3日",
    summary: "老城漫步",
    tags: ["经典"],
    pace: {
      level: "RELAXED",
      commute_status: "WITHIN_LIMIT",
      total_commute_minutes: 30,
    },
    accommodation,
    cost_estimate: {
      snapshot_version: "1",
      completeness: "complete",
      currency: "CNY",
      estimated_at: "2026-08-04T00:00:00Z",
      scenarios: [
        {
          scenario_id: "train",
          intercity_mode: "train",
          label: "高铁",
          total_scope: "full_trip",
          total_range: { min_cny: 1, max_cny: 2 },
          categories: [],
          missing_categories: [],
        },
      ],
      assumptions: [],
      exclusions: [],
      notice: "",
    },
    days: [
      {
        day: 1,
        title: "渝中",
        places: [
          {
            place_id: 1,
            name: "解放碑",
            category: "landmark",
            longitude: 106.57,
            latitude: 29.55,
            role: "anchor",
            optional: false,
            brief: "地标",
          },
        ],
        commute_legs: [],
        commute_summary: "",
        pace_status: "WITHIN_LIMIT",
        narrative: "",
      },
    ],
  };
}

function renderPlanView(plan: TripPlan) {
  const result: TripResult = {
    schema_version: "2.3",
    result_id: 1,
    city: { name: "重庆" },
    request: {
      days: 1,
      people_count: 1,
      preferences: [],
      avoid: [],
    },
    plans: [plan],
  };
  return render(
    <MemoryRouter>
      <MobilePlanView
        result={result}
        plan={plan}
        jobId="job"
        cityCovers={[]}
        timePrefText={null}
        packing={[]}
        tips={[]}
        transportSlot={null}
        costSlot={null}
        pdfLoading={false}
        onPdf={() => {}}
        onShare={() => {}}
        onBack={() => {}}
      />
    </MemoryRouter>,
  );
}
