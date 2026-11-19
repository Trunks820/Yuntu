/**
 * 两个「打开攻略」入口（/plan/:resultId/:planId、/result/:resultId）共用的空状态文案与错误分类。
 *
 * 契约背景：yuntu-bff `GET /api/trip/results/{result_record_id}` 的 `job_id` 是必填查询参数
 * （`Query(min_length=1, max_length=160)`）。旧书签、手改链接缺 `job_id` 时会拿到 FastAPI 的
 * 422 参数校验错误，前端不得把后端原文（如「请求失败」）当成用户能看懂的人话展示。
 * BFF 改这条契约时，这里的分类要一起改。
 */
import { ApiRequestError } from "@/services/api";

/** 链接缺少参数（或后端 422 参数校验失败）时的统一文案 */
export const MISSING_LINK_INFO_TEXT =
  "这个链接缺少信息，请从历史记录里重新打开。";

/** 攻略由旧契约版本生成、当前版本打不开时的文案 */
export const UNSUPPORTED_CONTRACT_TEXT =
  "该攻略由旧版本生成，暂不支持打开，请重新生成";

/**
 * notfound：404，攻略不存在；
 * unsupported：422 且 code=RESULT_CONTRACT_UNSUPPORTED，旧版本生成；
 * invalidlink：缺参数，或 422 参数校验失败（一律不展示后端原文）；
 * generic：其它错误，沿用既有后端 message。
 */
export type ResultOpenErrorKind =
  | "notfound"
  | "unsupported"
  | "invalidlink"
  | "generic";

export interface ResultOpenError {
  kind: ResultOpenErrorKind;
  message: string;
}

/** resultId / jobId 缺失时的空状态（两个入口共用同一段文案） */
export function missingLinkError(): ResultOpenError {
  return { kind: "invalidlink", message: MISSING_LINK_INFO_TEXT };
}

/**
 * 把 fetchResult 的失败统一映射成空状态。
 * 只有 404 / 旧版本契约 / 其它非 422 错误会沿用后端 message；
 * 422 参数类一律换成 MISSING_LINK_INFO_TEXT。
 */
export function classifyResultOpenError(err: unknown): ResultOpenError {
  if (err instanceof ApiRequestError) {
    if (err.status === 404) {
      return { kind: "notfound", message: "攻略不存在" };
    }
    if (err.status === 422) {
      if (err.code === "RESULT_CONTRACT_UNSUPPORTED") {
        return { kind: "unsupported", message: UNSUPPORTED_CONTRACT_TEXT };
      }
      return missingLinkError();
    }
    return { kind: "generic", message: err.message };
  }
  return { kind: "generic", message: "加载失败" };
}
