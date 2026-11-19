import { getQuotaNotice } from "@/constants/delivery";
import type { PublicGenerationDelivery } from "@/types/trip";

interface QuotaSettlementNoticeProps {
  delivery?: PublicGenerationDelivery | null;
}

export function QuotaSettlementNotice({
  delivery,
}: QuotaSettlementNoticeProps) {
  if (!delivery || !delivery.quota_state) return null;

  const notice = getQuotaNotice(delivery.quota_state);
  if (!notice) return null;

  return (
    <div className="mt-2 text-center text-xs text-gray-500">
      {notice}
    </div>
  );
}
