<div align="center">

# 🧭 Yuntu (云图旅行)

**AI 旅行规划引擎源码 · 确定性路线排程 + 时间轴可行性校验 + 事实约束路书生成**

在线完整体验：**[kakarot8.com](https://kakarot8.com)**（账号体系、16+ 热门城市、完整规划与云端同步能力）

[![Live Demo](https://img.shields.io/badge/Live-kakarot8.com-0f766e.svg?style=flat-square)](https://kakarot8.com)
[![Author: Trunks820](https://img.shields.io/badge/Author-Trunks820-orange.svg?style=flat-square&logo=github)](https://github.com/Trunks820)
[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-Community-gold.svg?style=flat-square&logo=linux)](https://linux.do)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg?style=flat-square&logo=python)](https://www.python.org/)
[![React: 18](https://img.shields.io/badge/React-18-61DAFB.svg?style=flat-square&logo=react)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%2B-336791.svg?style=flat-square&logo=postgresql)](https://www.postgresql.org/)

<br/>

<img src="docs/screenshots/home.jpg" alt="云图首页：重庆行程规划入口" width="100%" />

<p>
  <img src="docs/screenshots/plan.jpg" alt="云图行程详情：重庆三日街区漫步" width="100%" />
</p>

</div>

---

## 🌟 为什么选择 Yuntu？

市面上的大多数“AI 旅行规划”应用往往存在三大硬伤：
- **地点幻觉**：编造已经倒闭或根本不存在的餐厅、打卡点。
- **路线荒谬**：同一天内让游客在城市东西两头来回折返，或忽略场馆闭馆日与夜间营业窗口。
- **价格与时间瞎编**：随口捏造门票价格、酒店房价和通勤耗时。

**Yuntu 采用“确定性核心 + 创造性外围”的生产级架构（已同步至 v0.15.4 内核）**：
- **纯 SQL 真实地点池**：地点、经纬度、营业时间表（`opening_schedule`）与商圈归属全部来自本地 PostgreSQL 权威库，**绝不交由 LLM 凭空捏造 POI**。
- **两段式选点与空间时间轴排程（Selector + Route Planner）**：
  - 先基于出行偏好（美食、夜景、自然、历史人文、亲子等）与必去点（`must_include`）做候选池配额选点；
  - 再结合高德真实路网耗时、营业时间窗口、午休/晚归节奏预算与同区聚类，排出精确到分钟的每日时间轴（`planned_time_ranges`），并在异常时自动回退到确定性排程兜底（`scheduled_fallback`）。
- **住宿商圈自动推导（Accommodation Resolver）**：根据每日核心 POI 空间重心推导通勤最省时的落脚片区（如解放碑一带），严禁无凭据编造具体酒店品牌或房价。
- **版式蓝图与双轨路书（Composition Blueprint + Article Writer）**：LLM 仅在“结构化事实载荷（Structured Evidence Payload）”与片段注册表（`poi_fragments`）约束下撰写连贯游记或精炼基础版路书；开头偏好兑现说明与最终排期表严格对账。
- **行前准备与费用估算（Pre-trip Advice + Cost Estimate）**：结合出行季节、目的地地形特性、跨城交通参考与本地门票/餐饮消费基准，自动生成打包清单与预算区间。
- **发布门禁（Publish Gate）**：确定性代码复核路线顺序、必去点兑现率、营业冲突与价格声明，违规片段自动触发局部修复（Targeted Repair）或安全渲染器（`SafePlanRenderer`）兜底。

---

## ✨ 前端与交互亮点 (`web/`)

- **桌面端旅行册（Spread Book 全面接管）**：
  - **四步启程页**：目的地选择、天数与作息节奏、偏好标签与灵感选点、必去项确认一气呵成，砍掉冗余旧版入口。
  - **城市灵感图鉴抽屉（POI Inspiration Drawer）**：出发前即可按分类浏览目的地真实高赞地点、入选理由与避坑提示，一键加入“必去清单”。
  - **装订等待与双页路书**：实时 SSE 进度透出；生成后以左右双页路书呈现每日时间轴、高德动态连线地图、住宿片区建议、行前准备与费用明细。
- **移动端渐进式探索流**：针对触屏打磨的卡片式引导与单手抽屉交互。
- **免登录开箱即用 + 浏览器本地行程库**：开源版前端全面去登录化，生成的行程自动持久化在浏览器 `localStorage`（“我的行程”），克隆仓库跑起来即可完整体验创建、查看、重命名、删除与 PDF/长图导出。

---

## 🏗️ 核心架构流程

```text
用户请求 (自然语言 / 旅行册四步表单 / 灵感必去点)
  │
  ▼
[ 1. 意图解析 (Intent Parser) ]
  解析目的地、天数、偏好维度、必去 POI、每日起止时间与午休窗口
  │
  ▼
[ 2. 权威地点召回与候选选点 (SQL Retrieval + Candidate Selector) ]
  从 PostgreSQL 检索真实 POI + 营业时间表，按偏好配额与空间覆盖筛选候选池
  │
  ▼
[ 3. 住宿商圈锚定 (Accommodation Resolver · 空间重心算法) ]
  推导到各日核心景点通勤最省时的住宿商圈 (支持用户指定或自动推荐)
  │
  ▼
[ 4. 路线排程与时间轴校验 (Route Planner + Timeline Scheduler) ]
  结合高德通勤矩阵、场馆开放窗口与夜间时段，生成每日顺序与精确到分钟的时间轴
  │
  ▼
[ 5. 偏好对账与版式蓝图 (Preference Notices + Composition Blueprint) ]
  对账必去点、夜景、美食正餐兑现情况，构建每日叙事蓝图与行前准备/费用载荷
  │
  ▼
[ 6. 路书生成与局部修复 (Article Writer + Targeted Repair) ]
  在严格事实载荷约束下生成游记式路书，自动注入住宿建议与偏好说明
  │
  ▼
[ 7. 发布门禁复核 (Publish Gate + SafePlanRenderer 兜底) ]
  校验地点顺序、事实一致性与价格禁令，输出最终结构化路书与 POI 片段
```

---

## 📦 开源范围与说明

想直接用完整线上能力，打开 **[https://kakarot8.com](https://kakarot8.com)**（含账号云同步、16+ 热门城市完整数据库与线上集群配置）。

本仓库提供**规划引擎后端与配套前端的完整源码**，方便本地自建、架构研究与二次开发。本地运行前需准备：

- Python 3.11+、Node.js 20+、PostgreSQL 16+
- 大模型 API Key（推荐单配一个 **DeepSeek** 或 **Google Gemini** Key 即可跑通全链路，也支持按角色混配）
- 高德开放平台 Key（后端算路需 **Web 服务 Key**；前端展示地图另需 **Web 端 JS API Key**）

> [!NOTE]
> **为什么开源种子库默认只内置了【重庆】？**
> 本仓库聚焦展示**生产级 AI 旅行规划引擎的架构与全链路代码实现**。为降低本地自建门槛，`sql/seed_chongqing.sql` 仅内置了**重庆**核心经过验证的真实 POI、经纬度与摘要数据，前端也默认聚焦重庆以免因缺少其他城市数据而报错。
> 如需扩展其他城市（如成都、西安、北京、上海等），只需往 `travel_canonical_place` / `travel_place_summary` 导入对应城市数据，并在 [`src/api/destinations.py`](src/api/destinations.py) 中放开对应城市即可。

---

## 🛠️ 本地快速启动

先克隆仓库并准备环境变量：

```bash
git clone https://github.com/Trunks820/Yuntu.git
cd Yuntu
cp .env.example .env
```

### 1. 启动后端 (`http://127.0.0.1:6666`)

```bash
pip install -r requirements.txt
python -m scripts.init_db
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 6666 --reload
```

### 2. 启动前端 (`http://localhost:3000`)

```bash
cd web
cp .env.example .env.development   # 按需填入 VITE_AMAP_KEY 与 VITE_AMAP_SECURITY_JS_CODE
npm install
npm run dev
```

> **安全提醒**：`docker-compose.yml` 与 `.env.example` 中的 `yuntu` / `yuntupassword` 仅供本地开发演示，且后端默认仅放行本机 IP；若需容器化或暴露至公网，请务必修改数据库口令并收紧网关鉴权。

---

## ⚙️ 模型与环境配置 (`.env`)

Yuntu 内置了**单 Key 自动路由降级机制**：你只需要配置**任意一家**全局 API Key（如 `DEEPSEEK_API_KEY` 或 `GEMINI_API_KEY`），系统会自动将意图解析（`intent`）、选点（`selector`）、排线（`route_planner`）、撰写（`writer`）与修复（`repair`）全部路由到该 Key 对应的可用模型上，无需逐个填写。

完整配置项见 [`.env.example`](.env.example)，最简配置如下：

```env
# 数据库连接（本地演示账号，生产环境请修改）
DATABASE_URL=postgresql+asyncpg://yuntu:yuntupassword@localhost:5432/yuntu_travel

# 方式 A：单 Key 快速启动 — DeepSeek（推荐，性价比高且稳定）
DEEPSEEK_API_KEY=your_deepseek_api_key

# 方式 B：单 Key 快速启动 — Google Gemini（默认路由至 gemini-3.8-flash）
# GEMINI_API_KEY=your_gemini_api_key

# 方式 C：按角色混用不同模型 / OpenAI 兼容网关（可选，不填则自动复用全局 Key）
# ROUTE_PLANNER_PROVIDER=gemini
# ROUTE_PLANNER_MODEL=gemini-3.8-flash
# ROUTE_PLANNER_API_KEY=your_gemini_key
# WRITER_PROVIDER=deepseek
# WRITER_MODEL=deepseek-chat
# WRITER_API_KEY=your_deepseek_key

# 地理与路线服务（高德 Web 服务 Key）
AMAP_API_KEY=your_amap_api_key
AMAP_ROUTE_ENABLED=true
```

---

## 📡 核心 API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/destinations` | 获取当前启用的目的地城市列表与元数据 |
| `GET` | `/trip/poi-inspiration` | 获取城市灵感图鉴 POI 列表（支持分类筛选与精选推荐） |
| `GET` | `/trip/poi-inspiration/cities` | 获取支持灵感图鉴的城市概览 |
| `GET` | `/trip/poi-catalog` | 获取规划选点目录与必去点检索 |
| `POST` | `/trip/poi-catalog/selection` | 校验并解析用户勾选的必去地点集合 |
| `POST` | `/trip/async` | **主生产入口**：提交异步旅行规划任务，返回 `job_id` |
| `GET` | `/trip/jobs/{job_id}` | 轮询异步任务状态与阶段进度 |
| `GET` | `/trip/jobs/{job_id}/stream` | SSE 流式实时推送规划阶段进度与最终结果 |
| `GET` | `/trip/results/{result_id}` | 获取完整结构化路书（含每日时间轴、POI 片段、住宿建议、行前准备与费用估算） |
| `POST` | `/trip` | 同步调试入口（直接调用规划工作流） |

---

## 📂 项目结构

```text
Yuntu/
├── web/                     # 🎨 前端工程 (React 18 + TypeScript + TailwindCSS + Vite)
│   ├── src/
│   │   ├── components/      # 旅行册双页路书、灵感图鉴抽屉、高德地图联动、行前与费用卡片
│   │   ├── pages/           # 桌面 SpreadStartPage 四步启程、装订等待、路书详情、移动端视图
│   │   └── utils/           # 浏览器本地行程存储 (guestTripHistory)、必去点与偏好工具
│   └── Dockerfile           # 前端 Nginx 容器构建文件
├── src/                     # ⚙️ 核心规划引擎与 FastAPI 服务
│   ├── agents/              # 意图解析、选点、空间排程、住宿推导、偏好对账、文案撰写与门禁复核
│   ├── api/                 # FastAPI 路由（异步任务、灵感图鉴、选点目录、结果查询与导出）
│   ├── cost_estimate/       # 行程费用区间估算引擎
│   ├── cost_reference/      # 城市门票、餐饮与交通参考基准
│   ├── db/                  # SQLAlchemy 异步模型与查询层
│   ├── export/              # PDF 与长图渲染导出引擎
│   ├── jobs/                # 异步任务队列与后台 Worker
│   ├── config.py            # 全局配置与特性开关
│   └── model_routes.py      # 多模型路由表与单 Key 自动回退逻辑
├── sql/
│   ├── schema.sql           # PostgreSQL 全量建表脚本（含营业时间表与住宿字段）
│   └── seed_chongqing.sql   # 重庆精选真实 POI、商圈与摘要种子数据
├── scripts/
│   ├── init_db.py           # 一键初始化数据库表结构与重庆种子数据
│   ├── trip.py              # 命令行端到端规划调试脚本
│   └── stress_test.py       # 规划引擎回归与压力测试
├── tests/                   # 前后端核心逻辑与开源单 Key 适配单元测试
├── docs/                    # 架构文档与界面截图
├── docker-compose.yml       # 本地容器编排参考
├── Dockerfile               # 后端 Python 3.11 镜像构建文件
└── README.md
```

---

## 👨‍💻 作者与交流 (Author & Connect)

由 **[Trunks820](https://github.com/Trunks820)** 设计与研发。

如果你对 **LLM 落地实战、确定性 Agent 架构设计、旅行/本地生活垂类 AI** 感兴趣，欢迎：
- 🌟 给项目点个 **Star** 支持一下！
- 💡 提交 [Issue](https://github.com/Trunks820/Yuntu/issues) 或 Pull Request 共同完善
- 🤝 GitHub 关注 **[@Trunks820](https://github.com/Trunks820)** 交流探讨

## 💖 鸣谢 (Acknowledgments)

- 特别鸣谢 **[LINUX DO](https://linux.do)** 社区及其充满探索与极客精神的佬友们！

---

## 📄 License

本项目基于 [MIT License](LICENSE) 协议开源。
