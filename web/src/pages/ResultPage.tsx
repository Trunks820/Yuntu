import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { PlanSummaryCard } from "@/components/result/PlanSummaryCard";
import { DetailSkeleton } from "@/components/skeleton/DetailSkeleton";
import { ShareDialog } from "@/components/share/ShareDialog";
import { SafeDeliveryNotice } from "@/components/result/SafeDeliveryNotice";
import { QuotaSettlementNotice } from "@/components/result/QuotaSettlementNotice";
import { useTripStore } from "@/stores/tripStore";
import { fetchResult } from "@/services/api";
import { useCityPhotos } from "@/hooks/useCityPhotos";
import {
  classifyResultOpenError,
  missingLinkError,
  type ResultOpenError,
} from "@/utils/planLinkError";
import type { TripResult } from "@/types/trip";
import {
  Share2,
  Heart,
  SlidersHorizontal,
  MapPin,
  Lightbulb,
  Link2Off,
  SearchX,
  FileClock,
  TriangleAlert,
  Map,
} from "lucide-react";

export default function ResultPage() {
  const { resultId } = useParams<{ resultId: string }>();
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job_id");
  const isShareParam = searchParams.get("share") === "1";
  const isFromHistory = searchParams.get("from") === "history";

  const queryParts: string[] = [];
  if (jobId) queryParts.push(`job_id=${encodeURIComponent(jobId)}`);
  if (isShareParam) queryParts.push("share=1");
  if (isFromHistory) queryParts.push("from=history");
  const queryString = queryParts.length > 0 ? `?${queryParts.join("&")}` : "";

  const navigate = useNavigate();
  const storeResult = useTripStore((s) => s.result);
  const setResult = useTripStore((s) => s.setResult);

  // store 缓存只在 resultId+jobId 与当前 URL 一致时才复用，否则按 URL 重新拉取，
  // 避免上一条 job 的结果污染当前页（如北京结果显示在上海页）
  const matched =
    storeResult &&
    storeResult.resultId === resultId &&
    storeResult.jobId === (jobId ?? "");

  const [result, setLocal] = useState<TripResult | null>(
    matched ? storeResult.data : null,
  );
  const images = useCityPhotos(result?.city.name ?? "");
  const [loading, setLoading] = useState(!matched);
  // notfound=攻略不存在(404)；unsupported=旧版本生成不兼容(422)；invalidlink=缺参数或422参数校验失败；generic=其他
  const [error, setError] = useState<ResultOpenError | null>(null);
  const [shareOpen, setShareOpen] = useState(isShareParam);

  useEffect(() => {
    if (isShareParam) {
      setShareOpen(true);
    }
  }, [isShareParam]);

  useEffect(() => {
    // resultId/jobId 变化时重置本地态，防止 React Router 同路由复用组件导致旧结果残留
    const cached = useTripStore.getState().result;
    const hit =
      cached && cached.resultId === resultId && cached.jobId === (jobId ?? "");
    setError(null);
    if (hit) {
      setLocal(cached.data);
      setLoading(false);
      return;
    }
    setLocal(null);
    // 旧书签/损坏链接：缺 resultId 或 job_id 时不发请求，也不把后端 422 原文当人话展示。
    // job_id 是 BFF 的必填查询参数（GET /api/trip/results/{result_record_id}）。
    if (!resultId || !jobId) {
      setError(missingLinkError());
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    fetchResult(resultId, jobId)
      .then((data) => {
        if (cancelled) return;
        setLocal(data);
        setResult(resultId, jobId, data);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(classifyResultOpenError(err));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [resultId, jobId, setResult]);

  // 单方案时自动跳转到详情页（跳过方案选择页）
  useEffect(() => {
    if (!result || result.plans.length !== 1) return;
    navigate(`/plan/${resultId}/${result.plans[0].plan_id}${queryString}`, {
      replace: true,
    });
  }, [result, resultId, navigate, queryString]);

  if (loading) return <DetailSkeleton />;

  if (error || !result || result.plans.length === 0) {
    const kind = error?.kind;
    // 缺参数的旧链接用中性 lucide 图标 + 人话；其它分支保持原有单一按钮行为
    const Icon =
      kind === "invalidlink"
        ? Link2Off
        : kind === "notfound"
          ? SearchX
          : kind === "unsupported"
            ? FileClock
            : error
              ? TriangleAlert
              : Map;
    return (
      <div className="empty-state animate-fade-in px-5">
        <span className="empty-state-icon" aria-hidden="true">
          <Icon size={40} strokeWidth={1.5} />
        </span>
        <p className="max-w-xs text-sm font-medium text-primary-700">
          {error?.message ?? "暂时无法生成方案，请稍后重试"}
        </p>
        {kind === "invalidlink" ? (
          <div className="mt-2 flex flex-wrap items-center justify-center gap-3">
            <button
              onClick={() => navigate("/history")}
              className="btn-primary"
            >
              回到历史记录
            </button>
            <button onClick={() => navigate("/")} className="btn-secondary">
              重新规划
            </button>
          </div>
        ) : (
          <button onClick={() => navigate("/")} className="btn-primary mt-2">
            {kind === "unsupported" ? "重新生成" : "重新规划"}
          </button>
        )}
      </div>
    );
  }

  if (result.plans.length === 1) return <DetailSkeleton />;

  const { city, request, plans } = result;

  return (
    <div className="relative min-h-screen">
      {/* 暖玻璃背景：城市图 + 暖白蒙版（与首页一套质感） */}
      <img
        src={images[0]}
        alt=""
        loading="lazy"
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 -z-10 h-full w-full object-cover object-top"
      />
      <div className="pointer-events-none fixed inset-0 -z-10 bg-gradient-to-b from-[rgba(255,254,249,0.85)] via-[rgba(247,245,239,0.92)] to-[#f7f5ef]" />

      <main className="relative mx-auto max-w-6xl px-4 py-6 sm:px-6 sm:py-8">
        {/* 操作栏 */}
        <div className="mb-6 flex flex-wrap justify-end gap-2 sm:gap-3">
          <button
            onClick={() => setShareOpen(true)}
            className="flex items-center rounded-full border border-primary-100 bg-white/50 px-4 py-2 text-sm font-medium text-primary-700 transition-colors hover:bg-primary-50 sm:px-5"
          >
            <Share2 size={14} className="mr-1.5 text-primary-500 sm:mr-2" aria-hidden="true" /> 分享
          </button>
          <button
            disabled
            title="即将上线"
            className="flex items-center rounded-full border border-primary-100 bg-white/50 px-4 py-2 text-sm font-medium text-gray-400 cursor-not-allowed opacity-70 sm:px-5"
          >
            <Heart size={14} className="mr-1.5 text-gray-300 sm:mr-2" aria-hidden="true" /> 收藏
          </button>
          <button
            onClick={() => navigate("/")}
            className="flex items-center rounded-full bg-primary-700 px-4 py-2 text-sm font-medium text-white shadow-lg transition-colors hover:bg-primary-600 sm:px-5"
          >
            <SlidersHorizontal size={14} className="mr-1.5 sm:mr-2" aria-hidden="true" /> 调整偏好
          </button>
        </div>

        {/* 标题 */}
        <div className="mb-8 text-center animate-fade-in sm:mb-10">
          <h1 className="mb-4 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 text-2xl font-extrabold text-gray-800 sm:mb-5 sm:gap-3 sm:text-4xl">
            为你生成了
            <span className="relative text-accent-500">
              {plans.length}
              <svg className="absolute -bottom-2 left-0 h-2 w-full text-accent-400/40" viewBox="0 0 100 10" preserveAspectRatio="none">
                <path d="M0,5 Q50,10 100,5" fill="none" stroke="currentColor" strokeWidth="4" />
              </svg>
            </span>
            个方案
          </h1>
          <div className="inline-flex items-center rounded-full border border-primary-100/60 bg-white/70 px-4 py-1.5 text-sm text-gray-500 shadow-sm sm:text-base">
            <MapPin size={14} className="mr-2 text-primary-600" aria-hidden="true" />
            {city.name} · {request.days}天 · {request.people_count}人
          </div>
        </div>

        {/* 方案卡片网格（拍立得风，依次淡入） */}
        <div className="stagger-fade mx-auto grid max-w-4xl grid-cols-1 gap-x-8 gap-y-10 sm:grid-cols-2">
          {plans.map((plan, i) => (
            <PlanSummaryCard
              key={plan.plan_id}
              plan={plan}
              image={images[i % images.length]}
              recommended={i === 0}
              index={i}
              onClick={() => navigate(`/plan/${resultId}/${plan.plan_id}${queryString}`)}
            />
          ))}
        </div>

        <div className="mx-auto mt-10 max-w-4xl space-y-4">
          <SafeDeliveryNotice result={result} jobId={jobId} />
          <QuotaSettlementNotice delivery={result.delivery} />
        </div>

        {/* 手写签名收尾 */}
        <div className="mt-14 text-center">
          <p className="signature-font text-2xl text-primary-500 opacity-80">好行程，值得慢慢挑 ✦</p>
          <p className="mt-3 flex items-center justify-center gap-1.5 text-sm text-gray-400">
            <Lightbulb size={14} className="text-gray-300" aria-hidden="true" />
            方案均由 AI 智能生成，可
            <button onClick={() => navigate("/")} className="font-medium text-primary-600 hover:underline">
              调整偏好
            </button>
            重新规划
          </p>
        </div>
      </main>
      <ShareDialog open={shareOpen} onClose={() => setShareOpen(false)} recordId={String(result.result_id)} jobId={jobId ?? undefined} />
    </div>
  );
}
