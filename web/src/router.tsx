/* eslint-disable react-refresh/only-export-components */
import { createBrowserRouter, Navigate } from "react-router-dom";
import { lazy, Suspense } from "react";
import App from "./App";
import { useIsMobileViewport } from "./hooks/useIsMobileViewport";

const SpreadStartPage = lazy(() => import("./pages/start/SpreadStartPage"));
const SpreadHistoryPage = lazy(() => import("./pages/history/SpreadHistoryPage"));
const PlanningPage = lazy(() => import("./pages/PlanningPage"));
const SpreadPlanningPage = lazy(() => import("./pages/planning/SpreadPlanningPage"));
const ResultPage = lazy(() => import("./pages/ResultPage"));
const PlanDetailPage = lazy(() => import("./pages/PlanDetailPage"));
const SpreadBookPage = lazy(() => import("./pages/book/SpreadBookPage"));
const MobileHomePage = lazy(() => import("./pages/mobile/MobileHomePage"));
const MobileExplorePage = lazy(() => import("./pages/mobile/MobileExplorePage"));
const MobileInspirationPage = lazy(() => import("./pages/mobile/MobileInspirationPage"));
const MobileTripsPage = lazy(() => import("./pages/mobile/MobileTripsPage"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage"));

/** 手机视口用手机页面，宽屏用桌面页面 */
function Responsive({ mobile, desktop }: { mobile: React.ReactNode; desktop: React.ReactNode }) {
  return <>{useIsMobileViewport() ? mobile : desktop}</>;
}

function ResponsiveHomePage() {
  return <Responsive mobile={<MobileHomePage />} desktop={<SpreadStartPage />} />;
}

function LazyPage({ children }: { children: React.ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-[40vh] flex-col items-center justify-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-[3px] border-primary-200 border-t-primary-500" />
          <p className="text-sm text-gray-400">加载中…</p>
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

export const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      {
        index: true,
        element: (
          <LazyPage>
            <ResponsiveHomePage />
          </LazyPage>
        ),
      },
      {
        path: "explore",
        element: (
          <LazyPage>
            <MobileExplorePage />
          </LazyPage>
        ),
      },
      {
        path: "explore/inspiration",
        element: (
          <LazyPage>
            <MobileInspirationPage />
          </LazyPage>
        ),
      },
      {
        path: "classic",
        element: <Navigate to="/" replace />,
      },
      {
        path: "login",
        element: <Navigate to="/" replace />,
      },
      {
        path: "auth/callback",
        element: <Navigate to="/" replace />,
      },
      {
        path: "history",
        element: (
          <LazyPage>
            <Responsive mobile={<MobileTripsPage />} desktop={<SpreadHistoryPage />} />
          </LazyPage>
        ),
      },
      {
        path: "profile",
        element: <Navigate to="/history" replace />,
      },
      {
        path: "planning/:jobId",
        element: (
          <LazyPage>
            <Responsive mobile={<PlanningPage />} desktop={<SpreadPlanningPage />} />
          </LazyPage>
        ),
      },
      {
        path: "result/:resultId",
        element: (
          <LazyPage>
            <ResultPage />
          </LazyPage>
        ),
      },
      {
        path: "plan/:resultId/:planId",
        element: (
          <LazyPage>
            <Responsive mobile={<PlanDetailPage />} desktop={<SpreadBookPage />} />
          </LazyPage>
        ),
      },
      {
        path: "demo/*",
        element: <Navigate to="/" replace />,
      },
      {
        path: "*",
        element: (
          <LazyPage>
            <NotFoundPage />
          </LazyPage>
        ),
      },
    ],
  },
]);
