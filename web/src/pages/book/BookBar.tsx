import { FileText, LoaderCircle, Share } from "lucide-react";
import type { BookBarPageId, BookSidePageId } from "@/utils/spreadBook";

const BAR_PAGES: readonly { id: BookBarPageId; label: string }[] = [
  { id: "guide", label: "攻略" },
  { id: "before", label: "出发前" },
  { id: "go", label: "怎么去 · 住哪" },
  { id: "cost", label: "花多少" },
];

export interface BookBarProps {
  activePage: BookSidePageId | null;
  pdfLoading: boolean;
  onTogglePage: (page: BookBarPageId) => void;
  onExportPdf: () => void;
  onOpenShare: () => void;
}

export function BookBar({
  activePage,
  pdfLoading,
  onTogglePage,
  onExportPdf,
  onOpenShare,
}: BookBarProps) {
  return (
    <div className="bk-float bk-bar" role="group" aria-label="这一趟">
      {BAR_PAGES.map(({ id, label }) => (
        <button
          key={id}
          type="button"
          data-page={id}
          aria-pressed={activePage === id}
          onClick={() => onTogglePage(id)}
        >
          {label}
        </button>
      ))}
      <i className="sep" aria-hidden="true" />
      <button
        type="button"
        disabled={pdfLoading}
        aria-busy={pdfLoading}
        onClick={onExportPdf}
      >
        {pdfLoading ? (
          <LoaderCircle
            size={14}
            className="bk-bar-icon is-spin"
            aria-hidden="true"
          />
        ) : (
          <FileText size={14} className="bk-bar-icon" aria-hidden="true" />
        )}
        {pdfLoading ? "导出中…" : "导出 PDF"}
      </button>
      <button type="button" onClick={onOpenShare}>
        <Share size={14} className="bk-bar-icon" aria-hidden="true" />
        分享长图
      </button>
    </div>
  );
}
