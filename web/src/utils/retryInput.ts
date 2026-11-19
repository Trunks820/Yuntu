import { getHistoryTrips } from "@/services/api";
import type { TripSubmitRequest } from "@/types/tripRequest";

/**
 * 统一获取原始重试请求数据
 * @param jobId 任务 ID
 * @returns 匹配的表单请求数据，如果不存在则返回 null
 */
export async function findOriginalRetryInput(jobId: string): Promise<TripSubmitRequest | null> {
  if (!jobId) return null;

  // 查历史记录
  let cursor: string | undefined;
  do {
    const response = await getHistoryTrips({ cursor, limit: 50 });
    const matched = response.items.find((item) => item.job_id === jobId);
    if (matched?.retry_input?.trip_request) {
      return matched.retry_input.trip_request;
    }
    cursor = response.next_cursor ?? undefined;
  } while (cursor);

  return null;
}
