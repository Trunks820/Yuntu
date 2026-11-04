import { lazy, Suspense, useEffect } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { ErrorBoundary } from "./components/ErrorBoundary";
import Header from "./components/layout/Header";
import { Toast } from "./components/feedback/Toast";
import { ArtifactTaskNotice } from "./components/feedback/ArtifactTaskNotice";
import { TripTaskNotice } from "./components/feedback/TripTaskNotice";
import { useIsMobileViewport } from "./hooks/useIsMobileViewport";
import { useOnline } from "./hooks/useOnline";
import { useShareImageTaskStore } from "./stores/shareImageTaskStore";
import { useTripTaskStore } from "./stores/tripTaskStore";

const MobileTaskToast = lazy(() => import("./components/mobile/MobileTaskToast"));

/** 沉浸式页面自带顶栏/全屏背景，隐藏全局 Header 避免双层栏 */
function isImmersiveRoute(pathname: string): boolean {
  return (
    pathname === "/" ||
    pathname === "/explore" ||
    pathname.startsWith("/explore/") ||
    pathname.startsWith("/planning/") ||
    pathname.startsWith("/plan/")
  );
}

export default function App() {
  const online = useOnline();
  const location = useLocation();
  const isMobileViewport = useIsMobileViewport();
  const immersive = isImmersiveRoute(location.pathname);
  const initShareImageStore = useShareImageTaskStore((s) => s.initStore);
  const initTripTaskStore = useTripTaskStore((s) => s.initStore);

  useEffect(() => {
    initShareImageStore();
    initTripTaskStore();
  }, [initShareImageStore, initTripTaskStore]);

  // 路由切换时自动重置滚动条至顶部，避免详情页返回首页时视口停留在下方导致表单被拉上去
  useEffect(() => {
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  }, [location.pathname]);

  return (
    <div className="min-h-screen bg-white font-body text-gray-800 selection:bg-primary-100">
      {!online && (
        <div className="fixed top-0 left-0 right-0 z-50 bg-red-500 px-4 py-2 text-center text-sm font-medium text-white shadow-md">
          网络连接已断开，请检查网络设置
        </div>
      )}
      {!immersive && <Header />}
      <div className={immersive ? undefined : "pt-14"}>
        <ErrorBoundary>
          <div key={location.pathname} className="page-enter">
            <Outlet />
          </div>
        </ErrorBoundary>
      </div>
      <Toast />
      {isMobileViewport ? (
        <Suspense fallback={null}>
          <MobileTaskToast />
        </Suspense>
      ) : (
        <>
          <ArtifactTaskNotice />
          <TripTaskNotice />
        </>
      )}
    </div>
  );
}
