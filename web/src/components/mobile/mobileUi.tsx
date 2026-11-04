import React, { useEffect, useRef } from "react";

/** 原型 PATHS 图标集（24×24 线性图标，样式由 .ym svg 统一控制） */
const PATHS = {
  arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
  back: <path d="m14 5-7 7 7 7" />,
  home: <path d="M4 11 12 4l8 7M6 9.5V20h4.5v-6h3v6H18V9.5" />,
  chevron: <path d="m9 5 7 7-7 7" />,
  check: <path d="m5 12 4 4L19 6" />,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  spark: (
    <path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4M18 4h4" />
  ),
  heart: (
    <path d="M20.7 5.8c-2-2-5.2-1.7-7.2.3L12 7.6l-1.5-1.5c-2-2-5.2-2.3-7.2-.3-2.1 2.1-1.8 5.4.3 7.5L12 21l8.4-7.7c2.1-2.1 2.4-5.4.3-7.5Z" />
  ),
  book: <path d="M12 5C8 2 3 4 3 4v16s5-2 9 1c4-3 9-1 9-1V4s-5-2-9 1ZM12 5v16" />,
  layers: (
    <>
      <path d="m4 5 13-2 3 15-13 2L4 5ZM4 9l-2 1 3 12 12-2" />
      <path d="m9 13 3-4 4 5" />
    </>
  ),
  compass: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="m16 8-2 6-6 2 2-6 6-2Z" />
    </>
  ),
  pin: (
    <>
      <path d="M19 10c0 6-7 11-7 11S5 16 5 10a7 7 0 1 1 14 0Z" />
      <circle cx="12" cy="10" r="2.3" />
    </>
  ),
  calendar: (
    <>
      <rect x="3" y="5" width="18" height="16" rx="3" />
      <path d="M7 3v5M17 3v5M3 11h18M7 15h2M13 15h2M7 18h2" />
    </>
  ),
  person: (
    <>
      <circle cx="12" cy="7" r="3.5" />
      <path d="M5 21v-2a7 7 0 0 1 14 0v2" />
    </>
  ),
  people: (
    <>
      <circle cx="9" cy="7" r="3" />
      <path d="M2 20v-2a7 7 0 0 1 14 0v2M16 4a3 3 0 0 1 0 6M18 13a5 5 0 0 1 4 5v2" />
    </>
  ),
  family: (
    <>
      <circle cx="7" cy="6" r="2.5" />
      <circle cx="17" cy="6" r="2.5" />
      <circle cx="12" cy="14" r="2" />
      <path d="M2 18v-3a5 5 0 0 1 6-5M22 18v-3a5 5 0 0 0-6-5M8 22v-2a4 4 0 0 1 8 0v2" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 6v6l4 2" />
    </>
  ),
  pen: <path d="m15 4 5 5M4 20l4-1L20 7a2 2 0 0 0-4-4L4 15v5ZM4 20h16" />,
  undo: (
    <>
      <path d="M4 10h9a7 7 0 0 1 0 14" transform="translate(0 -5)" />
      <path d="m7 2-4 3 4 4" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v6M12 7v.2" />
    </>
  ),
  warn: (
    <>
      <path d="M12 3.5 21 19H3L12 3.5Z" />
      <path d="M12 9v6M12 17.5v.5" />
    </>
  ),
  mail: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="3" />
      <path d="m4 7 8 6 8-6" />
    </>
  ),
  exit: <path d="M10 4H4v16h6M9 12h12m-5-5 5 5-5 5" />,
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m16 16 5 5" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1 1M18 18l1 1M5 19l1-1M18 6l1-1" />
    </>
  ),
  map: <path d="m9 4-6 2v14l6-2 6 2 6-2V4l-6 2-6-2ZM9 4v14M15 6v14" />,
} as const;

export type MobileIconName = keyof typeof PATHS;

export const Icon: React.FC<{ name: MobileIconName; className?: string }> = ({
  name,
  className,
}) => (
  <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
    {PATHS[name]}
  </svg>
);

export const Logo: React.FC = () => (
  <span className="brand">
    <span className="brand-mark">
      <svg viewBox="0 0 26 26" aria-hidden="true">
        <path
          d="M9 19H7a4 4 0 0 1-1-8M12 7c4-3 9 1 7 5 5 0 6 7 0 7H9"
          stroke="currentColor"
          fill="none"
          strokeWidth="1.8"
        />
        <circle cx="7" cy="6" r="2" fill="#e9b266" stroke="none" />
      </svg>
    </span>
    云途 <small>YUNTU</small>
  </span>
);

/** 把含 "\n" 的文案渲染成带 <br> 的行 */
export const Lines: React.FC<{ text: string }> = ({ text }) => (
  <>
    {text.split("\n").map((line, i) => (
      <React.Fragment key={i}>
        {i > 0 && <br />}
        {line}
      </React.Fragment>
    ))}
  </>
);

interface MobileSheetProps {
  title: string;
  onClose: () => void;
  closeLabel?: string;
  className?: string;
  children: React.ReactNode;
}

/**
 * 原型底部面板（.overlay + .sheet）：点遮罩或 Esc 关闭，Tab 焦点限制在面板内，
 * 关闭后焦点回到唤起它的元素。
 */
export const MobileSheet: React.FC<MobileSheetProps> = ({
  title,
  onClose,
  closeLabel = "关闭面板",
  className,
  children,
}) => {
  const sheetRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeRef.current?.focus({ preventScroll: true });

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !sheetRef.current) return;
      const focusable = Array.from(
        sheetRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => !(el as HTMLButtonElement).disabled && !el.closest("[hidden]"));
      if (!focusable.length) {
        e.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const inside = sheetRef.current.contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);

  return (
    <div
      className="overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <section
        ref={sheetRef}
        className={`sheet${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="sheet-handle" />
        <div className="sheet-title">
          <h3>{title}</h3>
          <button
            ref={closeRef}
            type="button"
            className="icon-btn"
            onClick={onClose}
            aria-label={closeLabel}
          >
            <Icon name="close" />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
};


