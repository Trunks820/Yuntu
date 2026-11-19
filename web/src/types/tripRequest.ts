import type {
  AccommodationInput,
  MustIncludeItem,
  RequestedCommuteMode,
  TripFormData,
} from "./form";

/**
 * 跨端通用的行程提交请求数据传输对象 (DTO)。
 *
 * 与旧桌面端 TripFormData 相比：
 * - start_date 与 end_date 均为选填。当手机端选择「日期未定」时，
 *   请求体中不包含这两个字段（属性省略，绝不传空字符串 ""，也不伪造今天）。
 * - 旧桌面端 TripFormData 的 start_date/end_date 为必填 string，
 *   天然完全兼容 TripSubmitRequest (TripFormData extends TripSubmitRequest)。
 */
export interface TripSubmitRequest {
  to_city: string;
  days: number;
  people_count: number;
  start_date?: string;
  end_date?: string;
  preferences?: string[];
  avoid?: string[];
  notes?: string;
  from_city?: string;
  budget?: number;
  must_include?: MustIncludeItem[];
  commute_mode?: RequestedCommuteMode;
  daily_start?: string;
  daily_end?: string;
  accommodation?: AccommodationInput;
}

export type {
  RequestedCommuteMode,
  MustIncludeItem,
  AccommodationInput,
  TripFormData,
};
