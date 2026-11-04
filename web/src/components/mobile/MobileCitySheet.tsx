import React, { useMemo, useState } from "react";
import { useDestinations } from "@/hooks/useDestinations";
import type { Destination } from "@/types/destination";
import { MobileSheet } from "./mobileUi";

const EMPTY_DESTINATIONS: Destination[] = [];

interface MobileCitySheetProps {
  destinations: Destination[];
  isSelected: (dest: Destination) => boolean;
  onSelect: (dest: Destination) => void;
  onClose: () => void;
  /** 目录状态说明（加载中、核验中、加载失败），显示在搜索框上方 */
  notice?: React.ReactNode;
  /** 目录尚未完成实时核验：城市只能看，不能选 */
  disabled?: boolean;
}

/** 原型「把心动，放在哪座城？」全部城市面板：带搜索，城市来自实时公开目录 */
export const MobileCitySheet: React.FC<MobileCitySheetProps> = ({
  destinations,
  isSelected,
  onSelect,
  onClose,
  notice,
  disabled = false,
}) => {
  const [searchQuery, setSearchQuery] = useState("");

  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return destinations;
    return destinations.filter(
      (d) =>
        d.name.toLowerCase().includes(q) ||
        (d.region && d.region.toLowerCase().includes(q)) ||
        (d.nameEn && d.nameEn.toLowerCase().includes(q)),
    );
  }, [destinations, searchQuery]);

  return (
    <MobileSheet title="把心动，放在哪座城？" onClose={onClose}>
      {notice}
      <label htmlFor="directory-search">搜索城市或地区</label>
      <input
        id="directory-search"
        className="city-search"
        type="search"
        autoComplete="off"
        placeholder="例如：大理、华东"
        value={searchQuery}
        onChange={(e) => setSearchQuery(e.target.value)}
      />
      {filtered.length ? (
        <div className="directory-grid">
          {filtered.map((dest) => (
            <button
              key={dest.id}
              type="button"
              className="directory-choice"
              aria-pressed={isSelected(dest)}
              disabled={disabled}
              onClick={() => onSelect(dest)}
            >
              {dest.name}
            </button>
          ))}
        </div>
      ) : (
        destinations.length > 0 && (
          <p className="directory-empty" role="status">
            没有找到这座城。试试其他城市名，或清空搜索看看全部目的地。
          </p>
        )
      )}
      {!disabled && destinations.length > 0 && (
        <p className="discovery-demo">共 {destinations.length} 城 · 来自实时公开目录。</p>
      )}
    </MobileSheet>
  );
};

/** 自己读取目录的选城面板（首页入口、收集灵感用）：打开时才读取；实时目录核验完成前城市不可选 */
export const MobileLiveCityPicker: React.FC<{
  onSelect: (dest: Destination) => void;
  onClose: () => void;
  selectedCityName?: string | null;
}> = ({ onSelect, onClose, selectedCityName = null }) => {
  const { data, isLoading, isError, isPlaceholderData, refetch } = useDestinations();
  const destinations = data?.destinations ?? EMPTY_DESTINATIONS;
  const verified = !isLoading && !isError && !isPlaceholderData && destinations.length > 0;

  let notice: React.ReactNode = null;
  if (isError) {
    notice = (
      <div className="paper-note state-note warn" role="alert">
        目的地目录加载失败，请重试。
        <button type="button" className="text-btn" onClick={() => refetch()}>
          重新加载
        </button>
      </div>
    );
  } else if (isLoading) {
    notice = (
      <p className="paper-note state-note" role="status">
        正在加载目的地...
      </p>
    );
  } else if (isPlaceholderData) {
    notice = (
      <p className="paper-note state-note" role="status">
        正在同步实时目的地目录，核验完成后方可选择...
      </p>
    );
  } else if (!destinations.length) {
    notice = (
      <p className="paper-note state-note" role="status">
        暂无可用的目的地
      </p>
    );
  }

  return (
    <MobileCitySheet
      destinations={destinations}
      isSelected={(dest) => dest.name === selectedCityName}
      onSelect={onSelect}
      onClose={onClose}
      notice={notice}
      disabled={!verified}
    />
  );
};


