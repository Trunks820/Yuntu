import React from "react";
import { Link } from "react-router-dom";
import { Icon, Logo } from "./mobileUi";

interface MobileTopBarProps {
  title?: string;
  onBack?: () => void;
  /** 草稿流程：返回键旁多一个回首页（草稿已自动保存，无需确认） */
  onHome?: () => void;
  /** 首页：左侧显示品牌而不是返回键 */
  brand?: boolean;
  /** 账号页的顶栏样式保留，游客模式不进入登录 */
  account?: boolean;
}

/** 游客顶栏：没有登录、历史或个人资料入口。 */
export const MobileTopBar: React.FC<MobileTopBarProps> = ({
  title,
  onBack,
  onHome,
  brand = false,
  account = false,
}) => {
  return (
    <header className={`topbar${account ? " u-topbar" : ""}`}>
      {brand ? (
        <Link to="/" aria-label="云途首页">
          <Logo />
        </Link>
      ) : onHome ? (
        <span className="topbar-nav">
          <button type="button" className="icon-btn" onClick={onBack} aria-label="返回上一步">
            <Icon name="back" />
          </button>
          <button type="button" className="icon-btn" onClick={onHome} aria-label="回到首页">
            <Icon name="home" />
          </button>
        </span>
      ) : (
        <button type="button" className="icon-btn" onClick={onBack} aria-label="返回上一步">
          <Icon name="back" />
        </button>
      )}
      <span className="top-caption">{account ? "云途" : title || ""}</span>
      <span className="u-login-trigger" aria-label="游客模式">
        <Icon name="person" />
        <span>游客</span>
      </span>
    </header>
  );
};
