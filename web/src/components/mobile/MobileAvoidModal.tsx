import React, { useState } from "react";
import { useMobileTripStore } from "@/stores/mobileTripStore";
import { MOBILE_AVOID_OPTIONS } from "@/types/mobile";
import type { MobileAvoidId } from "@/types/mobile";
import { Icon, MobileSheet } from "./mobileUi";

interface MobileAvoidModalProps {
  onClose: () => void;
}

/**
 * 原型「还有什么想叮嘱的？」面板。
 * 改动先存在本地快照里，点「收好这些叮嘱」才写回草稿；关闭 / Esc 视为放弃。
 */
export const MobileAvoidModal: React.FC<MobileAvoidModalProps> = ({ onClose }) => {
  const draft = useMobileTripStore((s) => s.draft);
  const setAvoidIds = useMobileTripStore((s) => s.setAvoidIds);
  const setRawNotes = useMobileTripStore((s) => s.setRawNotes);

  const [localAvoidIds, setLocalAvoidIds] = useState<MobileAvoidId[]>(draft.avoidIds);
  const [localRawNotes, setLocalRawNotes] = useState<string>(draft.rawNotes);

  const toggle = (id: MobileAvoidId) =>
    setLocalAvoidIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    );

  const handleConfirm = () => {
    setAvoidIds(localAvoidIds);
    setRawNotes(localRawNotes.slice(0, 300));
    onClose();
  };

  return (
    <MobileSheet title="还有什么想叮嘱的？" onClose={onClose}>
      <p>有特别在意的，再告诉云途。没有也没关系。</p>
      <div className="tag-row">
        {MOBILE_AVOID_OPTIONS.map((opt) => {
          const selected = localAvoidIds.includes(opt.id);
          return (
            <button
              key={opt.id}
              type="button"
              className="chip"
              aria-pressed={selected}
              onClick={() => toggle(opt.id)}
              style={{
                padding: 12,
                fontSize: 14,
                ...(selected ? { background: "#166c5b", color: "white" } : {}),
              }}
            >
              {selected ? "✓ " : ""}
              {opt.label}
            </button>
          );
        })}
      </div>
      <label htmlFor="mobile-raw-notes" style={{ display: "block", margin: "22px 0 10px", fontSize: 14 }}>
        再留一句话（选填）
      </label>
      <textarea
        id="mobile-raw-notes"
        className="hand-note"
        maxLength={300}
        value={localRawNotes}
        onChange={(e) => setLocalRawNotes(e.target.value)}
        placeholder="例如：想给同行的朋友过个生日。"
      />
      <p className="tiny">{localRawNotes.length} / 300 字 · 提交时原样附在规划说明里。</p>
      <button type="button" className="primary" onClick={handleConfirm}>
        <span>收好这些叮嘱</span>
        <Icon name="check" />
      </button>
    </MobileSheet>
  );
};


