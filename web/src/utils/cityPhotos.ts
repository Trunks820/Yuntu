import {
  BUNDLED_CITY_IMAGES,
  BUNDLED_FALLBACK_IMAGE,
  bundledCityImages,
  resolveCityAssetVariant,
  usesBundledCityAssets,
  withCityAssetBase,
} from "@/config/cityAssets";
import { CITY_PHOTO_CREDITS } from "@/utils/cityPhotoCredits.generated";

export { CITY_PHOTO_CREDITS };

export const CITY_IMAGES: Record<string, string[]> = {
  beijing: [
    "hanson-lu-_8EFj6ISA08-unsplash.jpg",
    "victor-he-0xn9T2cEigE-unsplash.jpg",
    "zhang-kaiyv-alGTmO0KvJI-unsplash.jpg",
    "zhang-kaiyv-yT_9tsThivo-unsplash.jpg",
    "zhang-kaiyv-z4whdrqkO40-unsplash.jpg",
    "commons-7941314.jpg",
    "commons-19989908.jpg",
    "commons-15912403.jpg",
  ],
  shanghai: [
    "freeman-zhou-oV9hp8wXkPE-unsplash.jpg",
    "hanny-naibaho-D7InODIWyK4-unsplash.jpg",
    "nuno-alberto-MykFFC5zolE-unsplash.jpg",
    "yifan-cong-GszVE92a5Rs-unsplash.jpg",
    "zhou-xian-7tFFO6Mq5L4-unsplash.jpg",
    "commons-75799783.jpg",
    "commons-80576248.jpg",
    "commons-92824436.jpg",
  ],
  chongqing: [
    "harrison-qi-E9dIbdSd7LU-unsplash.jpg",
    "zhang-qc-EyScjFlzvtg-unsplash.jpg",
    "albert-canite-RG2YD21o81E-unsplash.jpg",
    "albert-canite-vMM4_VA8ogw-unsplash.jpg",
    "andrea-sun-pMnpZayQJFE-unsplash.jpg",
    "commons-58923778.jpg",
    "commons-12364263.jpg",
    "commons-77016559.jpg",
  ],
  chengdu: [
    "bamboo-joe-EoVw0eEM4lQ-unsplash.jpg",
    "kev1n-z-794u6VhyNws-unsplash.jpg",
    "lingbo-huang-6xqN2TCCmvA-unsplash.jpg",
    "theodor-lundqvist-6Ox3fPG-qvo-unsplash.jpg",
    "declan-sun-jmJLdFpntps-unsplash.jpg",
    "commons-91889717.jpg",
    "commons-109407045.jpg",
    "commons-51977424.jpg",
  ],
  hangzhou: [
    "luobing-egNgMn5CS18-unsplash.jpg",
    "ming-han-low-5UjoDKlGETs-unsplash.jpg",
    "ming-han-low-DPbmezddUp0-unsplash.jpg",
    "zhao-yangjun-FCi_wNGm9_Y-unsplash.jpg",
    "zhu-edward-peq-khnWDbg-unsplash.jpg",
    "commons-7031998.jpg",
    "commons-61658605.jpg",
    "commons-49832170.jpg",
  ],
  xian: [
    "aoyu-zhang-KNpElyP6R20-unsplash.jpg",
    "jun-ren-m8z_AnmlcpU-unsplash.jpg",
    "jun-ren-GPWtsTz_lOc-unsplash.jpg",
    "yihan-wang-2cvRysXC8Hk-unsplash.jpg",
    "yux-xiang-zvVX7prwg4c-unsplash.jpg",
    "commons-48987868.jpg",
    "commons-53939293.jpg",
    "commons-49184245.jpg",
  ],
  nanjing: [
    "jennifer-chen-Pnc2Uxb7PG0-unsplash.jpg",
    "kenneth-yang-lJWJLkwIsng-unsplash.jpg",
    "tianyang-zheng-OFoheOWxi2Y-unsplash.jpg",
    "dendy-X8EPQsYL754-unsplash.jpg",
    "cheng-shi-song-ARIhM3EBMHg-unsplash.jpg",
    "commons-91889218.jpg",
    "commons-11304194.jpg",
    "commons-21203193.jpg",
  ],
  changsha: [
    "zhe-zhang-TCaefwd87wE-unsplash.jpg",
    "steven-lynn-kCXcjXyVptM-unsplash.jpg",
    "bo-zhang-wd4o3QlBsdc-unsplash.jpg",
    "yihan-wang-obIg5fPvsBY-unsplash.jpg",
    "soulwinter-90myVe7VoYA-unsplash.jpg",
    "commons-114310766.jpg",
    "commons-112874738.jpg",
    "commons-64076455.jpg",
  ],
  qingdao: [
    "r-hai-BZrJ5tcuJE0-unsplash.jpg",
    "rockcyz-v6ceQ2Lj6b0-unsplash.jpg",
    "guxxxxyz-Fwo8xRfRSfM-unsplash.jpg",
    "hat-trick-obeOYXrKv7w-unsplash.jpg",
    "hat-trick-OQlBGJ4tSSc-unsplash.jpg",
    "commons-9521791.jpg",
    "commons-61780294.jpg",
    "commons-57715482.jpg",
  ],
  guilin: [
    "yma-KGauGIhyrjA-unsplash.jpg",
    "william-zhang--Qd91Sg6gZ8-unsplash.jpg",
    "chopsticks-on-the-loose-_75I7lCDgY8-unsplash.jpg",
    "jingyixiu-wmlj2DfZDkA-unsplash.jpg",
    "yma-SeFgGArj8Ls-unsplash.jpg",
    "commons-17045998.jpg",
    "commons-5607402.jpg",
    "commons-30184084.jpg",
  ],
  guangzhou: [
    "commons-27585080.jpg",
    "commons-352230.jpg",
    "commons-81350704.jpg",
    "commons-70706357.jpg",
    "commons-81350881.jpg",
    "commons-81350879.jpg",
    "commons-139342493.jpg",
    "commons-114128981.jpg",
  ],
  wuhan: [
    "commons-7673041.jpg",
    "commons-943956.jpg",
    "commons-15142424.jpg",
    "commons-9940033.jpg",
    "commons-9940037.jpg",
    "commons-9940268.jpg",
    "commons-20551084.jpg",
    "commons-11948820.jpg",
  ],
  suzhou: [
    "commons-8968704.jpg",
    "commons-48989129.jpg",
    "commons-47290500.jpg",
    "commons-75793999.jpg",
    "commons-91290162.jpg",
    "commons-91290169.jpg",
    "commons-91290182.jpg",
    "commons-47290539.jpg",
  ],
  xiamen: [
    "commons-8545184.jpg",
    "commons-56267835.jpg",
    "commons-8956304.jpg",
    "commons-120932674.jpg",
    "commons-19542121.jpg",
    "commons-82932880.jpg",
    "commons-130793291.jpg",
    "commons-81627855.jpg",
  ],
  kunming: [
    "commons-47122744.jpg",
    "commons-37669020.jpg",
    "commons-16129464.jpg",
    "commons-191736383.jpg",
    "commons-16129493.jpg",
    "commons-191736681.jpg",
    "commons-37673416.jpg",
    "commons-128042908.jpg",
  ],
  sanya: [
    "commons-54454491.jpg",
    "commons-61674412.jpg",
    "commons-57401233.jpg",
    "commons-48548423.jpg",
    "commons-57401415.jpg",
    "commons-68112830.jpg",
    "commons-57401499.jpg",
    "commons-108943217.jpg",
  ],
};

export const CITY_NAME_TO_FOLDER: Record<string, string> = {
  北京: "beijing",
  上海: "shanghai",
  重庆: "chongqing",
  成都: "chengdu",
  杭州: "hangzhou",
  西安: "xian",
  南京: "nanjing",
  长沙: "changsha",
  青岛: "qingdao",
  桂林: "guilin",
  广州: "guangzhou",
  武汉: "wuhan",
  苏州: "suzhou",
  厦门: "xiamen",
  昆明: "kunming",
  三亚: "sanya",
};

export const CITY_METADATA: Record<
  string,
  { pinyin: string; highlights: string[]; moodTag: string }
> = {
  重庆: {
    pinyin: "CHONGQING",
    highlights: ["洪崖洞", "解放碑", "十八梯", "李子坝轻轨"],
    moodTag: "赛博夜景 · 慢调寻味",
  },
  成都: {
    pinyin: "CHENGDU",
    highlights: ["大熊猫繁育基地", "锦里", "宽窄巷子", "太古里"],
    moodTag: "安逸巴适 · 熊猫漫活",
  },
  杭州: {
    pinyin: "HANGZHOU",
    highlights: ["西湖断桥", "灵隐寺", "西溪湿地", "法喜讲寺"],
    moodTag: "烟雨江南 · 诗意漫步",
  },
  西安: {
    pinyin: "XI'AN",
    highlights: ["兵马俑", "大唐不夜城", "大雁塔", "城墙骑行"],
    moodTag: "盛唐气象 · 碳水天堂",
  },
  北京: {
    pinyin: "BEIJING",
    highlights: ["故宫博物院", "颐和园", "南锣鼓巷", "景山公园"],
    moodTag: "古都文脉 · 庄严宏伟",
  },
  上海: {
    pinyin: "SHANGHAI",
    highlights: ["外滩万国建筑", "东方明珠", "武康路", "陆家嘴"],
    moodTag: "摩登魔都 · 梧桐树下",
  },
  南京: {
    pinyin: "NANJING",
    highlights: ["夫子庙秦淮河", "钟山风景区", "玄武湖", "鸡鸣寺"],
    moodTag: "六朝古都 · 金陵风雅",
  },
  长沙: {
    pinyin: "CHANGSHA",
    highlights: ["橘子洲头", "岳麓书院", "文和友", "太平老街"],
    moodTag: "不夜星城 · 香辣人间",
  },
  青岛: {
    pinyin: "QINGDAO",
    highlights: ["栈桥", "八大关", "奥帆中心", "五四广场"],
    moodTag: "红瓦绿树 · 碧海蓝天",
  },
  桂林: {
    pinyin: "GUILIN",
    highlights: ["漓江竹筏", "遇龙河", "象鼻山", "阳朔西街"],
    moodTag: "山水甲天下 · 仙境竹筏",
  },
  广州: {
    pinyin: "GUANGZHOU",
    highlights: ["广州塔", "沙面岛", "永庆坊", "上下九步行街"],
    moodTag: "粤韵风华 · 寻味早茶",
  },
  武汉: {
    pinyin: "WUHAN",
    highlights: ["黄鹤楼", "东湖绿道", "户部巷", "江汉路"],
    moodTag: "江城气魄 · 烟火江湖",
  },
  苏州: {
    pinyin: "SUZHOU",
    highlights: ["拙政园", "平江路", "虎丘", "金鸡湖"],
    moodTag: "姑苏水巷 · 园林雅趣",
  },
  厦门: {
    pinyin: "XIAMEN",
    highlights: ["鼓浪屿", "环岛路", "南普陀寺", "沙坡尾"],
    moodTag: "海上花园 · 海风微醺",
  },
  昆明: {
    pinyin: "KUNMING",
    highlights: ["滇池海埂", "翠湖公园", "官渡古镇", "斗南花市"],
    moodTag: "春城花语 · 鸥遇滇池",
  },
  三亚: {
    pinyin: "SANYA",
    highlights: ["亚龙湾", "蜈支洲岛", "天涯海角", "后海村"],
    moodTag: "热带椰林 · 碧海椰风",
  },
};

/**
 * 根据城市名和行程唯一 ID 计算稳定的 CDN 高清封面图
 */
export function getCityPostcardCover(city: string, seedKey = "default"): string {
  // 查找匹配的城市拼音/目录
  const cleanCity = city.trim().replace(/市$/, "");
  const folder = CITY_NAME_TO_FOLDER[cleanCity] || CITY_NAME_TO_FOLDER[city];

  if (usesBundledCityAssets()) {
    const localImages = folder ? bundledCityImages(folder) : [BUNDLED_FALLBACK_IMAGE];
    return localImages[0] ?? BUNDLED_FALLBACK_IMAGE;
  }

  if (folder && CITY_IMAGES[folder]?.length) {
    const images = CITY_IMAGES[folder];
    // 简单的字符串哈希，让不同 trip_id 分配到同一城市不同的真实风光图
    let hash = 0;
    for (let i = 0; i < seedKey.length; i++) {
      hash = (hash << 5) - hash + seedKey.charCodeAt(i);
      hash |= 0;
    }
    const index = Math.abs(hash) % images.length;
    const rawPath = `/city/${folder}/${images[index]}`;
    return resolveCityAssetVariant(rawPath, { format: "webp" });
  }

  // 默认兜底图
  return withCityAssetBase("/hero-bg.jpg");
}

export function getCityAllCovers(city: string): string[] {
  const cleanCity = city.trim().replace(/市$/, "");
  const folder = CITY_NAME_TO_FOLDER[cleanCity] || CITY_NAME_TO_FOLDER[city];

  if (usesBundledCityAssets()) {
    return folder ? bundledCityImages(folder) : [BUNDLED_FALLBACK_IMAGE];
  }

  if (folder && CITY_IMAGES[folder]?.length) {
    return CITY_IMAGES[folder].map((img) =>
      resolveCityAssetVariant(`/city/${folder}/${img}`, { format: "webp" })
    );
  }
  return [withCityAssetBase("/hero-bg.jpg")];
}

export function getCityPostcardMeta(city: string) {
  const cleanCity = city.trim().replace(/市$/, "");
  return (
    CITY_METADATA[cleanCity] ||
    CITY_METADATA[city] || {
      pinyin: cleanCity.toUpperCase(),
      highlights: ["热门地标", "特色体验", "城市打卡"],
      moodTag: "定制路线 · 探索未知",
    }
  );
}

/** 16 大官方名城的权威每日动线模版 */
export const CITY_DAY_ROUTES: Record<string, string[]> = {
  重庆: [
    "解放碑 ➔ 戴家巷 ➔ 洪崖洞夜景",
    "李子坝轻轨 ➔ 鹅岭二厂 ➔ 观音桥好吃街",
    "白象居 ➔ 长江索道 ➔ 南滨路钟楼",
    "磁器口古镇 ➔ 渣滓洞 ➔ 龙门浩老街",
    "武隆天生三桥 ➔ 仙女山大草原",
    "大足石刻 ➔ 昌州古城",
    "金刀峡 ➔ 偏岩古镇",
  ],
  杭州: [
    "西湖断桥 ➔ 孤山 ➔ 灵隐寺祈福",
    "法喜寺 ➔ 龙井问茶 ➔ 钱江新城灯光秀",
    "西溪湿地 ➔ 拱宸桥 ➔ 桥西历史街区",
    "宋城千古情 ➔ 九溪十八涧",
    "良渚古城遗址 ➔ 良渚文化艺术中心",
    "千岛湖中心湖区 ➔ 天屿山观景",
    "富春江 ➔ 严子陵钓台",
  ],
  成都: [
    "大熊猫基地 ➔ 春熙路 ➔ 太古里",
    "武侯祠 ➔ 锦里古街 ➔ 宽窄巷子",
    "杜甫草堂 ➔ 人民公园鹤鸣茶社 ➔ 九眼桥夜景",
    "都江堰水利工程 ➔ 青城山前山",
    "金沙遗址博物馆 ➔ 东郊记忆",
    "三星堆博物馆 ➔ 广汉夜市",
    "西岭雪山 ➔ 安仁古镇",
  ],
  西安: [
    "秦始皇兵马俑 ➔ 华清宫长恨歌",
    "大雁塔 ➔ 大唐不夜城沉浸夜游",
    "古城墙骑行 ➔ 回民街碳水寻味",
    "陕西历史博物馆 ➔ 永兴坊",
    "小雁塔 ➔ 西安博物院 ➔ 湘子庙街",
    "华山风景名胜区一日纵览",
    "大唐芙蓉园 ➔ 曲江池遗址公园",
  ],
  北京: [
    "故宫博物院 ➔ 景山公园 ➔ 什刹海胡同",
    "八达岭长城 ➔ 鸟巢水立方夜景",
    "颐和园 ➔ 圆明园遗址 ➔ 清华北大周边",
    "天坛公园 ➔ 前门大街 ➔ 大栅栏",
    "中国国家博物馆 ➔ 王府井步行街",
    "恭王府 ➔ 南锣鼓巷 ➔ 钟鼓楼",
    "环球影城全天沉浸体验",
  ],
  上海: [
    "外滩万国建筑群 ➔ 南京路步行街 ➔ 陆家嘴三件套",
    "武康路安福路 Citywalk ➔ 静安寺 ➔ 新天地",
    "上海迪士尼度假区全天狂欢",
    "豫园 ➔ 城隍庙 ➔ 浦东美术馆",
    "思南公馆 ➔ 田子坊 ➔ 滨江绿道",
    "朱家角古镇 ➔ 淀山湖",
    "上海博物馆 ➔ 中华艺术宫",
  ],
  南京: [
    "中山陵 ➔ 美龄宫 ➔ 音乐台喂鸽子",
    "夫子庙秦淮风光 ➔ 老门东 ➔ 瞻园",
    "南京大屠杀遇难同胞纪念馆 ➔ 玄武湖",
    "总统府 ➔ 颐和路公馆区 ➔ 先锋书店",
    "牛首山文化旅游区 ➔ 佛顶宫",
    "鸡鸣寺 ➔ 明城墙 ➔ 锁金村寻味",
    "栖霞山 ➔ 燕子矶公园",
  ],
  长沙: [
    "橘子洲头 ➔ 岳麓山爱晚亭 ➔ 太平老街",
    "湖南省博物院 ➔ 潮宗街 ➔ 超级文和友 ➔ 解放西",
    "谢子龙影像馆 ➔ 李自健美术馆 ➔ 扬帆夜市",
    "烈士公园 ➔ 湖南米粉街 ➔ 南门口",
    "靖港古镇 ➔ 铜官窑古镇",
    "梅溪湖大剧院 ➔ 桃花岭公园",
    "杜甫江阁夜景 ➔ 坡子街",
  ],
  青岛: [
    "栈桥喂海鸥 ➔ 八大关红瓦绿树 ➔ 信号山俯瞰",
    "崂山仰口 ➔ 燕儿岛山公园 ➔ 五四广场灯光秀",
    "青岛啤酒博物馆 ➔ 大学路网红墙 ➔ 小鱼山",
    "小麦岛公园 ➔ 石老人海水浴场日出",
    "胶澳总督府 ➔ 圣弥厄尔大教堂",
    "金沙滩 ➔ 唐岛湾滨海公园",
    "竹岔岛海岛慢行",
  ],
  桂林: [
    "象鼻山公园 ➔ 东西巷 ➔ 两江四湖夜游",
    "漓江精华游船 ➔ 阳朔西街 ➔ 印象刘三姐",
    "遇龙河竹筏漂流 ➔ 十里画廊骑行 ➔ 兴坪古镇",
    "龙脊梯田平安寨 ➔ 壮寨红瑶风情",
    "银子岩溶洞 ➔ 世外桃源景区",
    "七星景区 ➔ 芦笛岩",
    "冠岩景区 ➔ 古东瀑布",
  ],
  广州: [
    "越秀公园五羊雕像 ➔ 沙面岛 ➔ 永庆坊非遗街区",
    "广州塔小蛮腰 ➔ 珠江夜游 ➔ 广东省博物馆",
    "圣心大教堂 ➔ 北京路步行街 ➔ 陈家祠",
    "长隆野生动物世界全天欢聚",
    "东山口潮人街区 ➔ 荔湾湖公园",
    "白云山摩星岭 ➔ 云台花园",
    "海心沙亚运公园 ➔ 猎德大桥夜景",
  ],
  武汉: [
    "黄鹤楼登高 ➔ 武汉长江大桥 ➔ 粮道街过早",
    "湖北省博物馆 ➔ 东湖绿道骑行 ➔ 江汉路步行街",
    "晴川阁 ➔ 黎黄陂路街头博物馆 ➔ 汉口江滩",
    "古德寺 ➔ 汉正街 ➔ 武汉轮渡过江",
    "木兰草原 ➔ 云雾山",
    "光谷步行街 ➔ 汤逊湖",
    "归元禅寺 ➔ 月湖风景区",
  ],
  苏州: [
    "拙政园 ➔ 苏州博物馆 ➔ 平江路摇橹船",
    "虎丘塔 ➔ 寒山寺 ➔ 金鸡湖东方之门夜景",
    "留园 ➔ 山塘街夜景 ➔ 诚品书店",
    "周庄古镇 ➔ 双桥水巷夜色",
    "同里古镇 ➔ 退思园",
    "狮子林 ➔ 沧浪亭 ➔ 观前街",
    "东山太湖风景区 ➔ 陆巷古村",
  ],
  厦门: [
    "鼓浪屿日光岩 ➔ 菽庄花园 ➔ 最美转角",
    "南普陀寺 ➔ 环岛路骑行 ➔ 沙坡尾艺术西区",
    "钟鼓索道 ➔ 厦门大学周边 ➔ 八市海鲜寻味",
    "植物园雨林世界 ➔ 白城沙滩落日",
    "集美学村 ➔ 嘉庚公园 ➔ 龙舟池",
    "海上自行车道 ➔ 曾厝垵",
    "翔安下石井 ➔ 澳头渔村",
  ],
  昆明: [
    "滇池海埂大坝 ➔ 西山龙门索道俯瞰",
    "翠湖公园 ➔ 陆军讲武堂 ➔ 昆明老街",
    "斗南花卉市场 ➔ 官渡古镇",
    "石林风景名胜区一日奇观",
    "云南民族村 ➔ 民族博物馆",
    "九乡风景区溶洞群",
    "抚仙湖禄充风景区",
  ],
  三亚: [
    "蜈支洲岛潜水 ➔ 后海村冲浪体验",
    "亚龙湾热带天堂森林公园 ➔ 太阳湾沿海公路",
    "天涯海角 ➔ 鹿回头俯瞰全景 ➔ 亿恒夜市",
    "南山文化旅游区 ➔ 108米海上观音",
    "分界洲岛 ➔ 呆呆岛打卡",
    "三亚千古情 ➔ 椰梦长廊绝美日落",
    "西岛渔村慢调生活",
  ],
};

/** 获取某城市指定天数的每日动线 */
export function getCityDayRoutes(city: string, days = 3): { day: number; route: string }[] {
  const cleanCity = city.trim().replace(/市$/, "");
  const routes = CITY_DAY_ROUTES[cleanCity] || CITY_DAY_ROUTES[city] || [
    "核心地标打卡 ➔ 经典特色体验",
    "历史文化探索 ➔ 热门商圈寻味",
    "自然风光漫步 ➔ 璀璨夜景沉浸",
    "城市地道体验 ➔ 特色街区漫游",
    "周边特色胜景 ➔ 深度文化感知",
    "休闲慢调探索 ➔ 伴手礼选购",
    "全景风貌回顾 ➔ 返程时光",
  ];

  const actualDays = Math.min(7, Math.max(1, days));
  return Array.from({ length: actualDays }, (_, i) => ({
    day: i + 1,
    route: routes[i % routes.length],
  }));
}


const FOLDER_TO_CITY_NAME: Record<string, string> = Object.fromEntries(
  Object.entries(CITY_NAME_TO_FOLDER).map(([name, folder]) => [folder, name]),
);

const CITY_GALLERY_STEM_SET: ReadonlySet<string> = new Set(
  Object.entries(CITY_IMAGES).flatMap(([folder, files]) =>
    files.map((file) => `${folder.toLowerCase()}/${file.replace(/\.[^.]+$/, "")}`),
  ),
);

/**
 * 将已登记 R2 城市图库的高清图（city-opt/*.webp 或 /city/*.jpg）转为列表缩略小图（*.mobile.webp）。
 * 仅对 manifest 已发布的 128 张图库原图转换，poi-opt/ 及其他未登记路径原样返回。
 */
export function getCityMobilePhotoUrl(url: string): string {
  if (!url) return url;
  if (url === BUNDLED_FALLBACK_IMAGE || url.startsWith("/bundled/")) {
    return url;
  }

  const optMatch = url.match(
    /^(.*\/city-opt\/([a-z]+)\/([^/?#]+?))\.webp((?:[?#].*)?)$/i,
  );
  if (optMatch) {
    const [, prefix, folder, rawStem, suffix] = optMatch;
    if (/\.mobile$/i.test(rawStem)) {
      return url;
    }
    const key = `${folder.toLowerCase()}/${rawStem}`;
    return CITY_GALLERY_STEM_SET.has(key)
      ? `${prefix}.mobile.webp${suffix}`
      : url;
  }

  const legacyMatch = url.match(
    /^(?:https?:\/\/[^/?#]+)?(\/city\/([a-z]+)\/([^/?#]+?))\.jpg((?:[?#].*)?)$/i,
  );
  if (legacyMatch) {
    const [, , folder, stem, suffix] = legacyMatch;
    const key = `${folder.toLowerCase()}/${stem}`;
    if (CITY_GALLERY_STEM_SET.has(key)) {
      const resolved = resolveCityAssetVariant(`/city/${folder}/${stem}.jpg`, {
        mobile: true,
        format: "webp",
      });
      return suffix ? `${resolved}${suffix}` : resolved;
    }
  }

  return url;
}

/**
 * 根据图片 URL 或路径查询城市图库署名；地点实拍图（poi-opt/）标地点名。
 */
export function getCityPhotoCredit(
  urlOrPath: string | null | undefined,
  options?: { city?: string; placeName?: string },
): string {
  if (!urlOrPath) {
    return options?.placeName || options?.city || "";
  }
  if (urlOrPath === BUNDLED_FALLBACK_IMAGE || urlOrPath.startsWith("/bundled/")) {
    return options?.placeName || (options?.city ? `${options.city} · 内置实景图` : "重庆 · 内置实景图");
  }
  if (urlOrPath.includes("/poi-opt/") && options?.placeName) {
    return options.placeName;
  }
  const match = urlOrPath.match(
    /\/city(?:-opt)?\/([a-z]+)\/([^/?#]+?)(?:\.mobile)?\.(?:webp|jpe?g|png)$/i,
  );
  if (match) {
    const [, folder, stem] = match;
    const key = `${folder.toLowerCase()}/${stem}`;
    if (CITY_PHOTO_CREDITS[key]) {
      return CITY_PHOTO_CREDITS[key];
    }
    const cityName = options?.city || FOLDER_TO_CITY_NAME[folder.toLowerCase()] || folder;
    return `${cityName} · 实景图库`;
  }
  if (options?.placeName) {
    return options.placeName;
  }
  if (options?.city) {
    return `${options.city} · 实景图库`;
  }
  return "";
}

/**
 * 获取某城市的 8 张高清实景图及完整署名
 */
export function getCityGalleryItems(
  city: string,
): Array<{ url: string; credit: string }> {
  const cleanCity = city.trim().replace(/市$/, "");
  const folder = CITY_NAME_TO_FOLDER[cleanCity] || CITY_NAME_TO_FOLDER[city];
  if (usesBundledCityAssets()) {
    const bundled = folder ? BUNDLED_CITY_IMAGES[folder] : undefined;
    if (bundled?.length) {
      return bundled.map((url) => ({
        url,
        credit: getCityPhotoCredit(url, { city: cleanCity }),
      }));
    }
    return [];
  }
  if (folder && CITY_IMAGES[folder]?.length) {
    return CITY_IMAGES[folder].map((img) => {
      const url = resolveCityAssetVariant(`/city/${folder}/${img}`, {
        format: "webp",
      });
      const credit = getCityPhotoCredit(url, { city: cleanCity });
      return { url, credit };
    });
  }
  return [];
}



