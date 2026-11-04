import type { AsyncSubmitResponse, JobResponse, TripResult } from "@/types/trip";
import type { TripFormData } from "@/types/form";
import type { TripSubmitRequest } from "@/types/tripRequest";

const MOCK_JOB_ID = "mock-job-001";
const MOCK_RESULT_ID = "mock-result-001";

let pollCount = 0;

export function resetMock() {
  pollCount = 0;
}

export async function mockSubmitTrip(
  formData: TripFormData | TripSubmitRequest,
): Promise<AsyncSubmitResponse> {
  resetMock();
  await delay(400);

  if (formData.accommodation?.name) {
    MOCK_RESULT.plans.forEach((plan) => {
      plan.accommodation = {
        name: formData.accommodation!.name,
        latitude: plan.days[0]?.places[0]?.latitude ?? 29.5574,
        longitude: plan.days[0]?.places[0]?.longitude ?? 106.5784,
        source: "user_specified",
      };
    });
  }
  return { ok: true, job_id: MOCK_JOB_ID };
}

export async function mockPollJobStatus(
  _jobId: string,
): Promise<JobResponse> {
  await delay(300);
  pollCount++;

  if (pollCount <= 2) {
    return { ok: true, job_id: MOCK_JOB_ID, status: "RUNNING", stage_progress: { code: "ANALYZING", step: 1, total: 4 }, result_record_id: null, error: null };
  }
  if (pollCount <= 4) {
    return { ok: true, job_id: MOCK_JOB_ID, status: "RUNNING", stage_progress: { code: "PLANNING", step: 2, total: 4 }, result_record_id: null, error: null };
  }
  if (pollCount <= 6) {
    return { ok: true, job_id: MOCK_JOB_ID, status: "RUNNING", stage_progress: { code: "COMPOSING", step: 3, total: 4 }, result_record_id: null, error: null };
  }
  if (pollCount <= 8) {
    return { ok: true, job_id: MOCK_JOB_ID, status: "RUNNING", stage_progress: { code: "FINALIZING", step: 4, total: 4 }, result_record_id: null, error: null };
  }

  return { ok: true, job_id: MOCK_JOB_ID, status: "COMPLETED", stage_progress: { code: "FINALIZING", step: 4, total: 4 }, result_record_id: MOCK_RESULT_ID, error: null };
}

export async function mockFetchResult(
  _resultId: string,
): Promise<TripResult> {
  await delay(300);
  return MOCK_RESULT;
}

function delay(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

const MOCK_RESULT: TripResult = {
  schema_version: "1.0",
  result_id: 999,
  city: { name: "重庆" },
  request: { days: 3, people_count: 1, preferences: ["美食", "citywalk"], avoid: ["太累"] },
  weather: {
    status: "ok",
    city: "重庆",
    days: [
      { day: 1, date: "2026-08-05", weather_text: "晴", temp_min_c: 28, temp_max_c: 39, wind_text: "东南风 2级", icon_code: "sun", reminders: ["天气较热，请准备防晒"] },
      { day: 2, date: "2026-08-06", weather_text: "多云", temp_min_c: 27, temp_max_c: 37, wind_text: "微风 1级", icon_code: "cloud", reminders: [] },
      { day: 3, date: "2026-08-07", weather_text: "雷阵雨", temp_min_c: 25, temp_max_c: 32, wind_text: "北风 3级", icon_code: "rain", reminders: ["第3天有阵雨，出行请携带雨伞"] },
    ],
  },
  plans: [
    {
      plan_id: "plan_a",
      title: "轻松经典路线",
      summary: "经典地标与老街体验，融合渝中老城风情",
      tags: ["轻松", "经典", "citywalk"],
      pace: { level: "RELAXED", commute_status: "WITHIN_LIMIT", total_commute_minutes: 65 },
      cost_estimate: {
        snapshot_version: "1",
        completeness: "complete",
        currency: "CNY",
        estimated_at: "2026-08-04T00:00:00Z",
        scenarios: [
          {
            scenario_id: "train_round_trip",
            intercity_mode: "train",
            label: "高铁往返方案",
            total_scope: "full_trip",
            total_range: { min_cny: 1200, max_cny: 1800 },
            categories: [
              { category: "intercity_transport", coverage: "priced", range: { min_cny: 300, max_cny: 340 }, price_basis: "sourced", basis_label: "实时车次方案" },
              { category: "accommodation", coverage: "priced", range: { min_cny: 400, max_cny: 700 }, price_basis: "reference", basis_label: "解放碑舒适型酒店" },
              { category: "local_transport", coverage: "priced", range: { min_cny: 100, max_cny: 160 }, price_basis: "reference", basis_label: "市内公共交通与打车" },
              { category: "admission", coverage: "priced", range: { min_cny: 100, max_cny: 200 }, price_basis: "sourced", basis_label: "行程景点门票" },
              { category: "meals", coverage: "priced", range: { min_cny: 300, max_cny: 400 }, price_basis: "reference", basis_label: "餐饮消费参考" },
            ],
            missing_categories: [],
          },
          {
            scenario_id: "flight_round_trip",
            intercity_mode: "flight",
            label: "机票往返方案",
            total_scope: "full_trip",
            total_range: { min_cny: 1800, max_cny: 2400 },
            categories: [
              { category: "intercity_transport", coverage: "priced", range: { min_cny: 900, max_cny: 940 }, price_basis: "reference", basis_label: "机票参考价" },
              { category: "accommodation", coverage: "priced", range: { min_cny: 400, max_cny: 700 }, price_basis: "reference", basis_label: "解放碑舒适型酒店" },
              { category: "local_transport", coverage: "priced", range: { min_cny: 100, max_cny: 160 }, price_basis: "reference", basis_label: "市内公共交通与打车" },
              { category: "admission", coverage: "priced", range: { min_cny: 100, max_cny: 200 }, price_basis: "sourced", basis_label: "行程景点门票" },
              { category: "meals", coverage: "priced", range: { min_cny: 300, max_cny: 400 }, price_basis: "reference", basis_label: "餐饮消费参考" },
            ],
            missing_categories: [],
          },
        ],
        assumptions: [{ code: "two_travellers_per_room", label: "每间房两位旅客" }],
        exclusions: [{ code: "cycling_cost_not_included", label: "骑行费用暂未计入" }],
        notice: "费用为规划参考，实际支付金额请以预订或现场结算为准",
      },
      /**
       * v0.9.3 大交通推荐 Mock 数据
       * 默认：双模式 [train, flight]
       * 纯 flight 形态校验可通过取消下方 train 注释或保留单 flight 数组验证
       */
      transport: {
        from_city: "成都",
        to_city: "重庆",
        query_date: "2026-08-05",
        source: "realtime",
        modes: [
          {
            mode: "train",
            min_duration_minutes: 90,
            price_range: "¥154-168",
            price_source: "realtime",
            daily_count: 45,
            data_source: "realtime",
            availability_status: "available_at_query",
            availability_checked_at: "2026-08-02T10:00:00Z",
            options: [
              {
                type: "train",
                no: "G8501",
                departure_time: "08:00",
                arrival_time: "09:30",
                duration_minutes: 90,
                price: "¥154",
                departure_station: "成都东",
                arrival_station: "重庆北",
                airline: null,
              },
              {
                type: "train",
                no: "G8503",
                departure_time: "09:15",
                arrival_time: "10:45",
                duration_minutes: 90,
                price: "¥168",
                departure_station: "成都东",
                arrival_station: "重庆北",
                airline: null,
              },
            ],
          },
          {
            mode: "flight",
            min_duration_minutes: 60,
            price_range: "¥450(参考价)",
            price_source: "static_reference",
            daily_count: 8,
            data_source: "static_fallback",
            availability_status: "unknown",
            availability_checked_at: null,
            options: [
              {
                type: "flight",
                no: "CA4101",
                departure_time: "11:00",
                arrival_time: "12:00",
                duration_minutes: 60,
                price: null,
                departure_station: "T2",
                arrival_station: "T3",
                airline: "国航",
              },
            ],
          },
        ],
      },
      accommodation: {
        name: "解放碑/洪崖洞商圈",
        latitude: 29.5574,
        longitude: 106.5784,
        source: "auto_recommended",
        reason: "位于城市核心枢纽，步行可达十八梯与洪崖洞，交通极其便利",
      },
      days: [
        {
          day: 1,
          title: "渝中老城漫步",
          commute_summary: "当天以步行为主，总通勤约 25 分钟",
          pace_status: "WITHIN_LIMIT",
          narrative: "今天从解放碑出发，沿十八梯步行至洪崖洞，感受重庆最经典的山城地标。晚上在洪崖洞看夜景，吊脚楼亮灯后是拍照的最佳时机。",
          places: [
            { place_id: 1, name: "解放碑", category: "landmark", longitude: 106.5784, latitude: 29.5574, role: "anchor", optional: false, brief: "重庆地标，商圈核心" },
            { place_id: 2, name: "十八梯", category: "culture", longitude: 106.5753, latitude: 29.5538, role: "anchor", optional: false, brief: "山城老街巷，市井生活缩影" },
            { place_id: 3, name: "洪崖洞", category: "landmark", longitude: 106.5827, latitude: 29.5631, role: "anchor", optional: false, brief: "吊脚楼建筑群，夜景绝佳" },
          ],
          commute_legs: [
            { from_place_id: 1, to_place_id: 2, mode: "walking", duration_minutes: 10, distance_meters: 700 },
            { from_place_id: 2, to_place_id: 3, mode: "walking", duration_minutes: 15, distance_meters: 1100 },
          ],
        },
        {
          day: 2,
          title: "南岸江景与涂鸦",
          commute_summary: "公交+步行为主，总通勤约 40 分钟",
          pace_status: "WITHIN_LIMIT",
          narrative: "上午去南滨路看两江交汇，下午到川美涂鸦街拍照打卡，傍晚在交通茶馆喝盖碗茶，体验老重庆的慢生活。",
          places: [
            { place_id: 4, name: "南滨路", category: "scenic", longitude: 106.5726, latitude: 29.5453, role: "anchor", optional: false, brief: "两江交汇的江景步道" },
            { place_id: 5, name: "川美涂鸦街", category: "culture", longitude: 106.5429, latitude: 29.5301, role: "anchor", optional: false, brief: "整条街都是艺术涂鸦" },
            { place_id: 6, name: "交通茶馆", category: "food", longitude: 106.5380, latitude: 29.5275, role: "filler", optional: true, brief: "老重庆盖碗茶，50年不变" },
          ],
          commute_legs: [
            { from_place_id: 4, to_place_id: 5, mode: "transit", duration_minutes: 25, distance_meters: 4200 },
            { from_place_id: 5, to_place_id: 6, mode: "walking", duration_minutes: 8, distance_meters: 550 },
          ],
        },
        {
          day: 3,
          title: "磁器口古镇半日",
          commute_summary: "轻轨+步行，总通勤约 30 分钟",
          pace_status: "WITHIN_LIMIT",
          narrative: "最后一天逛磁器口古镇，尝陈麻花和火锅底料，适合慢慢逛。下午可以去鹅岭二厂看看文创园区。",
          places: [
            { place_id: 7, name: "磁器口古镇", category: "culture", longitude: 106.4483, latitude: 29.5793, role: "anchor", optional: false, brief: "千年古镇，山城老味道" },
            { place_id: 8, name: "鹅岭二厂", category: "culture", longitude: 106.5325, latitude: 29.5525, role: "filler", optional: true, brief: "老厂房改造的文创园区" },
          ],
          commute_legs: [
            { from_place_id: 7, to_place_id: 8, mode: "transit", duration_minutes: 30, distance_meters: 9500 },
          ],
        },
      ],
    },
  ],
};
