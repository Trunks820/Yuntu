import { useDestinations } from "@/hooks/useDestinations";
import { cityPhotoSources } from '@/utils/cityPhotoSources';
/* eslint-disable react-refresh/only-export-components */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  BUNDLED_FALLBACK_IMAGE,
  bundledCityImages,
  cityAssetPathname,
  resolveCityAssetVariant,
  usesBundledCityAssets,
} from '@/config/cityAssets';

const FALLBACK_IMAGE = '/city-placeholder.svg';
const ROTATE_INTERVAL_MS = 15000;
const FADE_MS = 500;
const LOAD_TIMEOUT_MS = 8000;
const MAX_CONSECUTIVE_ERRORS = 3;

const CITY_IMAGES: Record<string, string[]> = {
  beijing: [
    'hanson-lu-_8EFj6ISA08-unsplash.jpg',
    'victor-he-0xn9T2cEigE-unsplash.jpg',
    'zhang-kaiyv-alGTmO0KvJI-unsplash.jpg',
    'zhang-kaiyv-yT_9tsThivo-unsplash.jpg',
    'zhang-kaiyv-z4whdrqkO40-unsplash.jpg',
    'commons-7941314.jpg',
    'commons-19989908.jpg',
    'commons-15912403.jpg',
  ],
  shanghai: [
    'freeman-zhou-oV9hp8wXkPE-unsplash.jpg',
    'hanny-naibaho-D7InODIWyK4-unsplash.jpg',
    'nuno-alberto-MykFFC5zolE-unsplash.jpg',
    'yifan-cong-GszVE92a5Rs-unsplash.jpg',
    'zhou-xian-7tFFO6Mq5L4-unsplash.jpg',
    'commons-75799783.jpg',
    'commons-80576248.jpg',
    'commons-92824436.jpg',
  ],
  chongqing: [
    'harrison-qi-E9dIbdSd7LU-unsplash.jpg',
    'zhang-qc-EyScjFlzvtg-unsplash.jpg',
    'albert-canite-RG2YD21o81E-unsplash.jpg',
    'albert-canite-vMM4_VA8ogw-unsplash.jpg',
    'andrea-sun-pMnpZayQJFE-unsplash.jpg',
    'commons-58923778.jpg',
    'commons-12364263.jpg',
    'commons-77016559.jpg',
  ],
  chengdu: [
    'bamboo-joe-EoVw0eEM4lQ-unsplash.jpg',
    'kev1n-z-794u6VhyNws-unsplash.jpg',
    'lingbo-huang-6xqN2TCCmvA-unsplash.jpg',
    'theodor-lundqvist-6Ox3fPG-qvo-unsplash.jpg',
    'declan-sun-jmJLdFpntps-unsplash.jpg',
    'commons-91889717.jpg',
    'commons-109407045.jpg',
    'commons-51977424.jpg',
  ],
  hangzhou: [
    'luobing-egNgMn5CS18-unsplash.jpg',
    'ming-han-low-5UjoDKlGETs-unsplash.jpg',
    'ming-han-low-DPbmezddUp0-unsplash.jpg',
    'zhao-yangjun-FCi_wNGm9_Y-unsplash.jpg',
    'zhu-edward-peq-khnWDbg-unsplash.jpg',
    'commons-7031998.jpg',
    'commons-61658605.jpg',
    'commons-49832170.jpg',
  ],
  xian: [
    'aoyu-zhang-KNpElyP6R20-unsplash.jpg',
    'jun-ren-m8z_AnmlcpU-unsplash.jpg',
    'jun-ren-GPWtsTz_lOc-unsplash.jpg',
    'yihan-wang-2cvRysXC8Hk-unsplash.jpg',
    'yux-xiang-zvVX7prwg4c-unsplash.jpg',
    'commons-48987868.jpg',
    'commons-53939293.jpg',
    'commons-49184245.jpg',
  ],
  nanjing: [
    'jennifer-chen-Pnc2Uxb7PG0-unsplash.jpg',
    'kenneth-yang-lJWJLkwIsng-unsplash.jpg',
    'tianyang-zheng-OFoheOWxi2Y-unsplash.jpg',
    'dendy-X8EPQsYL754-unsplash.jpg',
    'cheng-shi-song-ARIhM3EBMHg-unsplash.jpg',
    'commons-91889218.jpg',
    'commons-11304194.jpg',
    'commons-21203193.jpg',
  ],
  changsha: [
    'zhe-zhang-TCaefwd87wE-unsplash.jpg',
    'steven-lynn-kCXcjXyVptM-unsplash.jpg',
    'bo-zhang-wd4o3QlBsdc-unsplash.jpg',
    'yihan-wang-obIg5fPvsBY-unsplash.jpg',
    'soulwinter-90myVe7VoYA-unsplash.jpg',
    'commons-114310766.jpg',
    'commons-112874738.jpg',
    'commons-64076455.jpg',
  ],
  qingdao: [
    'r-hai-BZrJ5tcuJE0-unsplash.jpg',
    'rockcyz-v6ceQ2Lj6b0-unsplash.jpg',
    'guxxxxyz-Fwo8xRfRSfM-unsplash.jpg',
    'hat-trick-obeOYXrKv7w-unsplash.jpg',
    'hat-trick-OQlBGJ4tSSc-unsplash.jpg',
    'commons-9521791.jpg',
    'commons-61780294.jpg',
    'commons-57715482.jpg',
  ],
  guilin: [
    'yma-KGauGIhyrjA-unsplash.jpg',
    'william-zhang--Qd91Sg6gZ8-unsplash.jpg',
    'chopsticks-on-the-loose-_75I7lCDgY8-unsplash.jpg',
    'jingyixiu-wmlj2DfZDkA-unsplash.jpg',
    'yma-SeFgGArj8Ls-unsplash.jpg',
    'commons-17045998.jpg',
    'commons-5607402.jpg',
    'commons-30184084.jpg',
  ],
  guangzhou: [
    'commons-27585080.jpg',
    'commons-352230.jpg',
    'commons-81350704.jpg',
    'commons-70706357.jpg',
    'commons-81350881.jpg',
    'commons-81350879.jpg',
    'commons-139342493.jpg',
    'commons-114128981.jpg',
  ],
  wuhan: [
    'commons-7673041.jpg',
    'commons-943956.jpg',
    'commons-15142424.jpg',
    'commons-9940033.jpg',
    'commons-9940037.jpg',
    'commons-9940268.jpg',
    'commons-20551084.jpg',
    'commons-11948820.jpg',
  ],
  suzhou: [
    'commons-8968704.jpg',
    'commons-48989129.jpg',
    'commons-47290500.jpg',
    'commons-75793999.jpg',
    'commons-91290162.jpg',
    'commons-91290169.jpg',
    'commons-91290182.jpg',
    'commons-47290539.jpg',
  ],
  xiamen: [
    'commons-8545184.jpg',
    'commons-56267835.jpg',
    'commons-8956304.jpg',
    'commons-120932674.jpg',
    'commons-19542121.jpg',
    'commons-82932880.jpg',
    'commons-130793291.jpg',
    'commons-81627855.jpg',
  ],
  kunming: [
    'commons-47122744.jpg',
    'commons-37669020.jpg',
    'commons-16129464.jpg',
    'commons-191736383.jpg',
    'commons-16129493.jpg',
    'commons-191736681.jpg',
    'commons-37673416.jpg',
    'commons-128042908.jpg',
  ],
  sanya: [
    'commons-54454491.jpg',
    'commons-61674412.jpg',
    'commons-57401233.jpg',
    'commons-48548423.jpg',
    'commons-57401415.jpg',
    'commons-68112830.jpg',
    'commons-57401499.jpg',
    'commons-108943217.jpg',
  ],
};

const CITY_NAME_TO_FOLDER: Record<string, string> = {
  北京: 'beijing',
  上海: 'shanghai',
  重庆: 'chongqing',
  成都: 'chengdu',
  杭州: 'hangzhou',
  西安: 'xian',
  南京: 'nanjing',
  长沙: 'changsha',
  青岛: 'qingdao',
  桂林: 'guilin',
  广州: 'guangzhou',
  武汉: 'wuhan',
  苏州: 'suzhou',
  厦门: 'xiamen',
  昆明: 'kunming',
  三亚: 'sanya',
};

const FOLDER_TO_CITY_NAME: Record<string, string> = {
  beijing: '北京',
  shanghai: '上海',
  chongqing: '重庆',
  chengdu: '成都',
  hangzhou: '杭州',
  xian: '西安',
  nanjing: '南京',
  changsha: '长沙',
  qingdao: '青岛',
  guilin: '桂林',
  guangzhou: '广州',
  wuhan: '武汉',
  suzhou: '苏州',
  xiamen: '厦门',
  kunming: '昆明',
  sanya: '三亚',
};


let isWebpSupported = true;
try {
  const elem = document.createElement('canvas');
  if (elem.getContext && elem.getContext('2d')) {
    isWebpSupported = elem.toDataURL('image/webp').indexOf('data:image/webp') === 0;
  }
} catch {
  // fallback to true
}

export function resolveOptimalUrl(url: string): string {
  // 根据当前视口宽度决定是否使用移动端小图
  const isMobile = window.matchMedia?.('(max-width: 768px)')?.matches ?? false;
  return resolveCityAssetVariant(url, {
    mobile: isMobile,
    format: isWebpSupported ? 'webp' : 'jpg',
  });
}

/** 取某城本地风景图 URL（明信片/等待页等共用） */
export function getCityPhotoUrls(city: string, count = 4): string[] {
  const folder = CITY_NAME_TO_FOLDER[city];
  if (usesBundledCityAssets()) {
    const imgs = folder ? bundledCityImages(folder) : [BUNDLED_FALLBACK_IMAGE];
    return Array.from({ length: count }, (_, idx) => imgs[idx % imgs.length]);
  }
  if (folder && CITY_IMAGES[folder]?.length) {
    return CITY_IMAGES[folder].slice(0, count).map((f) => resolveOptimalUrl(`/city/${folder}/${f}`));
  }
  return Array.from({ length: count }, () => FALLBACK_IMAGE);
}

export function cityNameOfImage(url: string): string | null {
  if (url === BUNDLED_FALLBACK_IMAGE || url.startsWith('/bundled/')) {
    return url.includes('chongqing') ? '重庆' : null;
  }
  const match = cityAssetPathname(url)?.match(/^\/city(?:-opt)?\/([^/]+)\//);
  return match ? (FOLDER_TO_CITY_NAME[match[1]] ?? null) : null;
}

/**
 * 取某城市的图片列表（用于给方案卡配图）。
 * 旧演示页使用；未配置 CDN 时回退内置资源，生产页面读取 BFF 目录图片。
 */
export function cityImageList(city: string): string[] {
  const entry = Object.entries(CITY_NAME_TO_FOLDER).find(
    ([name, folder]) => city.includes(name) || city.toLowerCase().includes(folder),
  );
  if (entry) {
    const folder = entry[1];
    if (usesBundledCityAssets()) {
      return bundledCityImages(folder);
    }
    return CITY_IMAGES[folder].map((f) => resolveOptimalUrl(`/city/${folder}/${f}`));
  }
  return [FALLBACK_IMAGE];
}

export function useRotatingBackground(cities: string[], fallbackMode: 'shuffle' | 'static' = 'shuffle') {
  const { data } = useDestinations();
  const cityKey = JSON.stringify(cities);
  const pool = useMemo(() => {
    const requestedCities: string[] = JSON.parse(cityKey);
    // Prefetch a directory photo while the form waits for fresh availability.
    // This is decorative only; it must not restore an unverified destination.
    const selectedCities = requestedCities.some(Boolean) || fallbackMode === 'static' ? requestedCities
      : [data?.destinations.find(item => item.isActive !== false)?.name ?? ''];
    const photos = selectedCities.flatMap(city => {
      const d = data?.destinations.find(item => item.name === city);
      return cityPhotoSources(city, d).filter(url => url !== FALLBACK_IMAGE);
    });
    return photos.length ? [...new Set(photos)] : [FALLBACK_IMAGE];
  }, [cityKey, data, fallbackMode]);

  const [current, setCurrent] = useState(FALLBACK_IMAGE);
  const [incoming, setIncoming] = useState<string | null>(null);
  const currentRef = useRef(current);
  currentRef.current = current;

  useEffect(() => {
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    setIncoming(null);
    let cancelled = false;
    let idx = 0;
    let activeImg: HTMLImageElement | null = null;
    let activeTimeout: ReturnType<typeof setTimeout> | null = null;
    let consecutiveErrors = 0;

    /** 清理上一次未完成的图片加载，防止旧回调干扰新请求 */
    const cleanupLoad = () => {
      if (activeImg) {
        activeImg.onload = null;
        activeImg.onerror = null;
        activeImg = null;
      }
      if (activeTimeout) {
        clearTimeout(activeTimeout);
        activeTimeout = null;
      }
    };

    const advance = () => {
      // 清理上一次未完成的加载，防止竞态
      cleanupLoad();

      // 连续失败过多时暂停，等下一轮 interval 再试
      if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
        consecutiveErrors = 0;
        return;
      }

      let url = pool[idx % pool.length];
      idx++;
      if (url === currentRef.current && pool.length > 1) {
        url = pool[idx % pool.length];
        idx++;
      }
      if (url === currentRef.current) return;

      const img = new Image();
      activeImg = img;
      const targetUrl = url;

      img.onload = () => {
        if (!cancelled && img === activeImg) {
          consecutiveErrors = 0;
          if (reducedMotion) setCurrent(targetUrl);
          else setIncoming(targetUrl);
        }
        cleanupLoad();
      };

      img.onerror = () => {
        if (!cancelled && img === activeImg) {
          consecutiveErrors++;
          advance(); // 跳到池中下一张
        }
      };

      // 超时降级：LOAD_TIMEOUT_MS 内未加载完则跳过这张
      activeTimeout = setTimeout(() => {
        if (!cancelled && img === activeImg) {
          consecutiveErrors++;
          advance(); // 尝试下一张（通常更小的图）
        }
      }, LOAD_TIMEOUT_MS);

      img.src = targetUrl;
    };

    // 城市切换后当前图不在池中时，立即换一张
    if (!pool.includes(currentRef.current)) advance();

    const timer = reducedMotion ? null : setInterval(advance, ROTATE_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
      cleanupLoad();
    };
  }, [pool]);

  useEffect(() => {
    if (!incoming || !pool.includes(incoming)) return;
    const t = setTimeout(() => {
      setCurrent(incoming);
      setIncoming(null);
    }, FADE_MS);
    return () => clearTimeout(t);
  }, [incoming, pool]);

  return { current, incoming: incoming && pool.includes(incoming) ? incoming : null };
}

export function RotatingBackground({
  current,
  incoming,
}: {
  current: string;
  incoming: string | null;
}) {
  return (
    <div className="fixed inset-0 z-0">
      <div
        className="absolute inset-0 bg-cover bg-center"
        style={{ backgroundImage: `url('${current}')` }}
      />
      {incoming && (
        <div
          className="absolute inset-0 bg-cover bg-center animate-bg-fade-in"
          style={{ backgroundImage: `url('${incoming}')` }}
        />
      )}
      <div className="absolute inset-0 bg-gradient-to-r from-white/90 via-white/55 to-white/10" />
    </div>
  );
}
