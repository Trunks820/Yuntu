import type { TripSubmitRequest } from "@/types/tripRequest";
import { Plane } from "lucide-react";

interface BoardingPassProps {
  city: string;
  formData: TripSubmitRequest | null;
  jobId?: string;
}

const CITY_IATA: Record<string, string> = {
  北京: "PEK",
  上海: "SHA",
  重庆: "CKG",
  成都: "CTU",
  杭州: "HGH",
  西安: "XIY",
  南京: "NKG",
  长沙: "CSX",
  青岛: "TAO",
  桂林: "KWL",
  广州: "CAN",
  武汉: "WUH",
  苏州: "SZV",
  厦门: "XMN",
  昆明: "KMG",
  三亚: "SYX",
};

export function BoardingPass({ city, formData, jobId }: BoardingPassProps) {
  const days = formData?.days ?? null;
  const people = formData?.people_count ?? 1;
  const iataCode = CITY_IATA[city] || "DEST";
  const serialNo = jobId ? jobId.replace(/^job_/, "").slice(0, 8).toUpperCase() : "YT-2026";

  return (
    <div className="relative w-[340px] max-w-full rotate-0 sm:rotate-[1.5deg] select-none transition-transform duration-500 hover:rotate-0">
      <div className="ticket-notch-mask rounded-2xl bg-white/95 backdrop-blur-2xl border border-white/80 shadow-[0_25px_50px_-12px_rgba(0,0,0,0.22),0_0_0_1px_rgba(0,0,0,0.05)] overflow-hidden">
        {/* 票根头部 */}
        <div className="flex items-center justify-between px-6 pt-5 pb-3 border-b border-gray-100/80 bg-sand-50/50">
          <div className="flex items-center gap-2">
            <img src="/logo.svg" alt="" className="h-6 w-6" aria-hidden="true" />
            <div>
              <p className="text-xs font-black tracking-wider text-primary-800 uppercase">
                YUNTU · 专属路书
              </p>
              <p className="text-[9px] font-mono tracking-widest text-gray-400">
                BOARDING PASS
              </p>
            </div>
          </div>
          <span className="font-mono text-[10px] font-semibold text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded-full border border-emerald-200/60">
            CONFIRMED
          </span>
        </div>

        {/* 航段与目的地 */}
        <div className="flex items-center justify-between px-6 py-4">
          <div>
            <p className="font-mono text-[10px] tracking-widest text-gray-400 font-semibold">ORIGIN</p>
            <p className="text-xl font-black text-gray-800 leading-tight">出发地</p>
            <p className="font-mono text-xs font-bold text-gray-400">DEP</p>
          </div>

          <div className="flex-1 mx-3 flex flex-col items-center justify-center">
            <span className="font-mono text-[9px] text-primary-500 tracking-wider font-semibold">
              FLT {serialNo}
            </span>
            <div className="w-full flex items-center text-primary-500 my-1">
              <span className="h-px flex-1 bg-gradient-to-r from-transparent to-primary-300" />
              <Plane size={12} className="mx-1.5 text-primary-600" aria-hidden="true" />
              <span className="h-px flex-1 bg-gradient-to-r from-primary-300 to-transparent border-dashed" />
            </div>
            <span className="text-[9px] text-gray-400 font-medium">AI 深度定制</span>
          </div>

          <div className="text-right">
            <p className="font-mono text-[10px] tracking-widest text-gray-400 font-semibold">DEST</p>
            <p className="text-xl font-black text-primary-700 leading-tight">{city}</p>
            <p className="font-mono text-xs font-bold text-primary-500">{iataCode}</p>
          </div>
        </div>

        {/* 撕裂分隔虚线（与真实凹槽同轴） */}
        <div className="relative my-0.5">
          <div className="border-t-2 border-dashed border-gray-200/80 mx-5" />
        </div>

        {/* 票根信息网格 */}
        <div className="grid grid-cols-3 gap-2 px-6 py-3.5 text-center bg-gray-50/40">
          <div>
            <p className="font-mono text-[9px] text-gray-400 tracking-wider">DAYS</p>
            <p className="text-sm font-black text-gray-800">{days ? `${days} 天` : "-"}</p>
          </div>
          <div>
            <p className="font-mono text-[9px] text-gray-400 tracking-wider">TRAVELERS</p>
            <p className="text-sm font-black text-gray-800">{people} 人</p>
          </div>
          <div>
            <p className="font-mono text-[9px] text-gray-400 tracking-wider">CLASS</p>
            <span className="inline-block text-xs font-black text-accent-600 bg-amber-50 px-2 py-0.5 rounded border border-amber-200/60 shadow-2xs">
              高定
            </span>
          </div>
        </div>

        {/* 真实感条形码与编号 */}
        <div className="px-6 pt-3 pb-4">
          <div
            className="h-8 rounded-xs opacity-80"
            style={{
              background:
                "repeating-linear-gradient(90deg, #1e293b 0 2px, transparent 2px 4px, #1e293b 4px 7px, transparent 7px 9px, #1e293b 9px 10px, transparent 10px 14px, #1e293b 14px 17px, transparent 17px 19px, #1e293b 19px 20px, transparent 20px 24px)",
            }}
            aria-hidden="true"
          />
          <div className="mt-1.5 flex items-center justify-between text-[9px] font-mono text-gray-400 tracking-wider">
            <span>* YT-{iataCode}-{serialNo} *</span>
            <span>SECURE AI VERIFIED</span>
          </div>
        </div>
      </div>
    </div>
  );
}
