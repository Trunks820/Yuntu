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
  /** 书架/信息页顶栏：标题用宋体「云途」并带下边线（原型 .u-topbar） */
  account?: boolean;
}

/** 手机端顶栏（开源免登录直连模式：右侧直达本地「我的行程」） */
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
      <Link to="/history" className="u-login-trigger" aria-label="我的行程">
        <Icon name="book" />
        <span>我的行程</span>
      </Link>
    </header>
  );
};
