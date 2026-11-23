<div align="center">

# 🧭 Yuntu (云图旅行)

**AI 旅行规划开源系统 · 真实地点选点 + 顺路时间表 + 事实受控的路书生成**

在线完整体验：**[kakarot8.com](https://kakarot8.com)**（支持账号云同步、27 座热门旅行城市、完整规划与导出能力）

[![Live Demo](https://img.shields.io/badge/Live-kakarot8.com-0f766e.svg?style=flat-square)](https://kakarot8.com)
[![Author: Trunks820](https://img.shields.io/badge/Author-Trunks820-orange.svg?style=flat-square&logo=github)](https://github.com/Trunks820)
[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-Community-gold.svg?style=flat-square&logo=linux)](https://linux.do)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg?style=flat-square&logo=python)](https://www.python.org/)
[![React: 18](https://img.shields.io/badge/React-18-61DAFB.svg?style=flat-square&logo=react)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%2B-336791.svg?style=flat-square&logo=postgresql)](https://www.postgresql.org/)

<br/>

<img src="docs/screenshots/home.jpg" alt="云图首页：四步启程与画面灵感" width="100%" />

<p>
  <img src="docs/screenshots/plan.jpg" alt="云图旅行册：地图路线连线与每日行程长卷" width="100%" />
</p>

</div>

---

## 🌟 为什么做 Yuntu？

很多人用大模型做旅行攻略，经常踩这三个坑：
- **瞎编地点**：推荐早就关门的餐厅，甚至根本不存在的景点。
- **路线折腾**：一天之内让你在城市东西两头来回跑四趟，或者去了才发现当天闭馆。
- **价格乱报**：随口编造门票、酒店房价和路上的通勤时间。

**Yuntu 的做法很实在：把“查地点、算路线、卡时间”交给确定的程序，只让大模型根据查好的事实写路书**：
- **地点只从真实数据库里挑**：所有景点、餐厅、经纬度和营业时间都存在本地 PostgreSQL 里，**绝不让大模型凭空编地点**。
- **路线顺路、时间表精确到分钟**：结合高德真实路网耗时、场馆开放时间、午休窗口与夜间时段，先把每天去哪、几点到几点、路上花多少分钟算清楚；万一模型排线超时，也有本地算法兜底排出一份顺路行程。
- **自动推荐住哪一片最省时**：根据每天要去的核心景点位置，算出通勤最方便的住宿商圈（比如“建议住在解放碑附近”），不乱编具体酒店名字和价格。
- **行前准备与花费参考**：结合出行季节、天气、跨城交通与当地消费水平，自动整理好必带物品清单、避坑提醒和费用估算。
- **出稿前先对账**：生成完路书后，程序会逐条核对景点顺序、必去点有没有漏、正餐和夜景有没有兑现，发现对不上的地方自动修正。

---

## ✨ 界面与体验亮点

- **桌面端“摊开的旅行册”**：
  - **四步出发**：挑心动画面 → 定旅行节奏 → 选目的地 → 定天数与同行人，一步只问一件事。
  - **灵感图鉴与必去清单**：出发前可以浏览目的地的真实景点照片、推荐理由和避坑提醒，遇到想去的一键加进“必去清单”。
  - **地图画布配每日长卷**：生成好的攻略直接铺在地图上，左上角看整趟概要，底部长卷按天展示每一站的时段、亮点和站间交通；点右上角还能展开完整游记攻略、出发前准备、交通住宿和费用明细。
- **手机端专属探索流（屏幕宽度 `< 768px` 自动切换）**：
  - **三段式入口**：首页支持滑动轮播切换「心动 / 节奏 / 同行」，提供三条起手方式——「挑几个心动画面」分步选感觉、「随手收集灵感」漫无目的逛景点，以及「已经想好去哪」直达目的地。自动保留未完成的草稿和正在生成的任务卡片。
  - **随手收集灵感（滑卡收藏）**：不用先选城市，像翻明信片一样左滑跳过、右滑把喜欢的真实地点收进本机「想去清单」，单城最多带上 5 个必去点直接开排；支持点按查看同一地点的多张实景照片。
  - **单手路书阅读**：按天左右滑动切页看每日行程，顶栏实时带出天气图标与出行提醒；点任意一站从当前卡片直接展开底部详情抽屉（支持下拉关闭），底栏随时一键打开当天路线地图或导出 A5 PDF 旅行册。

  <p align="center">
    <img src="docs/screenshots/mobile-home.jpg" alt="手机端首页与三段入口" width="31%" />
    <img src="docs/screenshots/mobile-inspiration.jpg" alt="手机端随手收集灵感滑卡" width="31%" />
    <img src="docs/screenshots/mobile-explore.jpg" alt="手机端挑心动画面分步卡片" width="31%" />
  </p>

- **免登录、本地直接存行程**：开源版桌面端与手机端均去掉了登录门槛，生成的行程、草稿和「想去清单」自动保存在浏览器本地，拉下代码跑起来就能完整体验生成、查看、导出 A5 旅行册 PDF 和分享长图。

---

## 🏗️ 一份攻略是怎么生成的？

```text
用户提交出行想法 (四步表单 / 自然语言 / 必去地点)
  │
  ▼
1. 读懂需求
   拆解目的地、玩几天、同行人数、偏好（美食/夜景/人文等）与作息时间
  │
  ▼
2. 从数据库挑候选地点
   从本地 PostgreSQL 取出真实景点与地道餐厅，附上网友推荐理由与营业时间
  │
  ▼
3. 算好住哪一片最方便
   根据候选景点的位置重心，算出往返最省时间的住宿商圈
  │
  ▼
4. 排定每日顺路路线与时间表
   结合高德通勤耗时与场馆开放窗口，排好每天的游览顺序与到站时段
  │
  ▼
5. 核对偏好并整理行前清单
   检查必去点、夜景、正餐有没有排上，同步生成天气提醒、打包清单与费用参考
  │
  ▼
6. 撰写路书并做最终检查
   基于排好的路线和真实资料写出每日游记攻略，检查无误后交付页面与 PDF/长图导出
```

---

## 📦 开源说明

如果你想直接体验完整功能，可以访问线上站 **[https://kakarot8.com](https://kakarot8.com)**（线上版目前已开放 **27 座热门旅行城市**，支持账号登录与云端同步）。

这个仓库包含**规划引擎后端和前端界面的完整源码**，方便你在本地运行、研究架构或二次开发。本地跑起来需要准备：

- Python 3.11+、Node.js 20+、PostgreSQL 16+
- 大模型 API Key（配一个 **DeepSeek** 或 **Google Gemini** 的 Key 就能跑通全部环节；不配 Key 也能走本地基础路线生成一份可用行程）
- 高德开放平台 Key（可选：后端算真实通勤需要 **Web 服务 Key**；前端显示互动地图需要 **Web 端 JS API Key**）

> [!NOTE]
> **开源版内置了哪些城市的数据？**
> 线上站已有 **27 座城市**，开源仓库这次放开了最具代表性的 **5 座旗舰城市全量种子数据（重庆、北京、上海、成都、杭州）**——由 [`sql/seed_chongqing.sql`](sql/seed_chongqing.sql) 与 [`sql/seed_cities.sql`](sql/seed_cities.sql) 共同构成，涵盖 **573 个高德核验 POI、50 个核心住宿商圈、客观推荐摘要与 340 张实景配图**；配套的费用目录与城市封图 JSON 也已同步瘦身至这 5 城，克隆后本地跑通完整选城、滑卡灵感、预算估算与导出链路，又不至于拖着几十兆无关数据。
> 如果你还想补充更多城市（比如西安、南京、厦门等），只要把对应城市的数据导入 `travel_canonical_place` / `travel_place_summary` 表，再在 [`src/api/destinations.py`](src/api/destinations.py) 里登记即可。

---

## 🛠️ 本地运行步骤

先克隆代码并复制配置文件：

```bash
git clone https://github.com/Trunks820/Yuntu.git
cd Yuntu
cp .env.example .env
```

### 1. 启动后端 (`http://127.0.0.1:6666`)

```bash
pip install -r requirements.txt

# 安装用于导出 A5 旅行册 PDF 和分享长图的无头浏览器（带系统依赖与中文字体）
playwright install --with-deps chromium
# 如果在云服务器上碰到默认下载源 403 或偏慢，可换国内镜像再装：
# PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright playwright install --with-deps chromium

# 初始化数据库表并导入 5 城全量种子数据（重庆、北京、上海、成都、杭州）
python -m scripts.init_db

# 启动后端服务
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 6666 --reload
```

### 2. 启动前端 (`http://localhost:3000`)

```bash
cd web
cp .env.example .env.development   # 如需前端显示高德底图，在里面填入 VITE_AMAP_KEY 和 VITE_AMAP_SECURITY
npm install
npm run dev
```

> **安全提醒**：`docker-compose.yml` 和 `.env.example` 里的 `yuntu` / `yuntupassword` 只是本地演示密码，而且后端默认只允许本机 IP 访问；如果要部署到公网，请记得改掉数据库密码并收紧访问权限。

---

## ⚙️ 模型与环境配置 (`.env`)

不用同时配好几家模型！你只需要在 `.env` 里填**任意一个**大模型 Key（推荐 `DEEPSEEK_API_KEY` 或 `GEMINI_API_KEY`），系统就会自动用这把 Key 跑完解析、选点、排线和写路书所有环节。

完整配置项可看 [`.env.example`](.env.example)，平时最常用的就这几项：

```env
# 数据库连接（本地演示账号，上生产请修改）
DATABASE_URL=postgresql+asyncpg://yuntu:yuntupassword@localhost:5432/yuntu_travel

# 方式 A：只配 DeepSeek（推荐，便宜稳定）
DEEPSEEK_API_KEY=your_deepseek_api_key

# 方式 B：只配 Google Gemini（默认使用 gemini-3.8-flash）
# GEMINI_API_KEY=your_gemini_api_key

# 方式 C：不同环节混用不同模型（可选，不填就自动复用上面的全局 Key）
# ROUTE_PLANNER_PROVIDER=gemini
# ROUTE_PLANNER_MODEL=gemini-3.8-flash
# ROUTE_PLANNER_API_KEY=your_gemini_key
# WRITER_PROVIDER=deepseek
# WRITER_MODEL=deepseek-chat
# WRITER_API_KEY=your_deepseek_key

# 高德 Web 服务 Key（用于计算景点之间的真实交通时间）
AMAP_API_KEY=your_amap_api_key
AMAP_ROUTE_ENABLED=true
```

---

## 📡 常用 API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/destinations` | 获取当前开放的目的地城市列表 |
| `GET` | `/trip/poi-inspiration` | 获取城市的灵感图鉴地点与实景图集 |
| `GET` | `/trip/poi-inspiration/cities` | 获取支持灵感图鉴的城市及地点数量 |
| `GET` | `/trip/poi-catalog` | 搜索和浏览城市的可选必去地点目录 |
| `GET` | `/trip/poi-catalog/selection` | 校验用户勾选的必去地点 |
| `POST` | `/trip/async` | **主入口**：提交异步规划任务，返回 `job_id`（没配模型 Key 时也能降级生成基础行程） |
| `GET` | `/trip/jobs/{job_id}` | 查询规划任务的当前状态与进度 |
| `GET` | `/trip/jobs/{job_id}/stream` | 实时推送规划进度的 SSE 流接口 |
| `GET` | `/trip/results/{result_id}` | 获取生成好的完整路书（含每日路线、时段、住宿建议、行前清单和费用估算） |
| `POST` | `/trip` | 命令行/调试用的同步生成接口（需要配置好大模型 Key） |

---

## 📂 目录结构

```text
Yuntu/
├── web/                     # 🎨 前端界面 (React 18 + TypeScript + TailwindCSS + Vite)
│   ├── src/
│   │   ├── components/      # 顶部栏、分享弹窗、行前建议与费用卡片等公共组件
│   │   ├── pages/           # 四步出发页 (start)、装订等待页 (planning)、旅行册画布 (book)、本地我的行程 (history) 及手机端页面
│   │   └── utils/           # 浏览器本地行程存储 (guestTripHistory) 与排期格式化工具
│   └── Dockerfile           # 前端容器构建文件
├── src/                     # ⚙️ 后端规划引擎与 API 服务
│   ├── agents/              # 需求解析、选点、路线时间表排程、住宿推荐、路书撰写与质量检查
│   ├── api/                 # FastAPI 路由接口（异步任务、灵感图鉴、地点目录、结果与导出）
│   ├── cost_estimate/       # 旅行花费估算模块
│   ├── cost_reference/      # 城市门票、餐饮与交通参考价数据
│   ├── db/                  # 数据库表模型与查询封装
│   ├── export/              # A5 旅行册 PDF 与分享长图渲染引擎
│   ├── jobs/                # 异步任务队列与后台 Worker
│   ├── config.py            # 全局配置项
│   └── model_routes.py      # 模型路由与单 Key 自动复用逻辑
├── sql/
│   ├── schema.sql           # PostgreSQL 建表脚本
│   ├── seed_chongqing.sql   # 重庆全量种子数据（136 个验证地点、10 个商圈与实景图集）
│   └── seed_cities.sql      # 北京/上海/成都/杭州全量种子数据（437 个验证地点、40 个商圈与实景图集）
├── scripts/
│   ├── init_db.py           # 一键建表并导入上述 5 城种子数据
│   ├── trip.py              # 命令行单次规划体验脚本
│   └── stress_test.py       # 批量回归与压力测试脚本
├── tests/                   # 自动化测试套件
├── docs/                    # 架构说明与界面截图
├── docker-compose.yml       # 本地 Docker 编排参考
├── Dockerfile               # 后端容器镜像构建文件
└── README.md
```

---

## 👨‍💻 作者与交流

由 **[Trunks820](https://github.com/Trunks820)** 设计与开发。

如果你对 **AI 落地实战、靠谱的 Agent 架构设计、旅行与本地生活应用** 感兴趣，欢迎：
- 🌟 给项目点个 **Star** 支持一下！
- 💡 提 [Issue](https://github.com/Trunks820/Yuntu/issues) 或 Pull Request 一起打磨
- 🤝 在 GitHub 关注 **[@Trunks820](https://github.com/Trunks820)** 交流想法

## 💖 鸣谢

- 特别感谢 **[LINUX DO](https://linux.do)** 社区和各位热心折腾、帮忙实测的佬友们！

---

## 📄 License

本项目基于 [MIT License](LICENSE) 协议开源。
