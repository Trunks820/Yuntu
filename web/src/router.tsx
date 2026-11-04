/* eslint-disable react-refresh/only-export-components */
import { createBrowserRouter } from "react-router-dom";
import { lazy, Suspense } from "react";
import App from "./App";
import { useIsMobileViewport } from "./hooks/useIsMobileViewport";

const CinematicHomepagePage = lazy(() => import("./pages/demo/CinematicHomepagePage"));
const MobileHomePage = lazy(() => import("./pages/mobile/MobileHomePage"));
const MobileExplorePage = lazy(() => import("./pages/mobile/MobileExplorePage"));
const MobileInspirationPage = lazy(() => import("./pages/mobile/MobileInspirationPage"));
const PlanningPage = lazy(() => import("./pages/PlanningPage"));
const ResultPage = lazy(() => import("./pages/ResultPage"));
const PlanDetailPage = lazy(() => import("./pages/PlanDetailPage"));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage"));

/** 手机视口用手机首页，宽屏继续用现有沉浸首页。 */
function ResponsiveHomePage() {
  const mobile = useIsMobileViewport();
  return mobile ? <MobileHomePage /> : <CinematicHomepagePage />;
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
        path: "planning/:jobId",
        element: (
          <LazyPage>
            <PlanningPage />
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
            <PlanDetailPage />
          </LazyPage>
        ),
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
