<div align="center">

# 🧭 Yuntu (云图旅行)

**AI 旅行规划引擎源码 · 确定性路线排程 + 事实约束文案生成**

在线完整体验：**[kakarot8.com](https://kakarot8.com)**（账号、更多城市、完整规划能力）

[![Live Demo](https://img.shields.io/badge/Live-kakarot8.com-0f766e.svg?style=flat-square)](https://kakarot8.com)
[![Author: Trunks820](https://img.shields.io/badge/Author-Trunks820-orange.svg?style=flat-square&logo=github)](https://github.com/Trunks820)
[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-Community-gold.svg?style=flat-square&logo=linux)](https://linux.do)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg?style=flat-square&logo=python)](https://www.python.org/)
[![React: 18](https://img.shields.io/badge/React-18-61DAFB.svg?style=flat-square&logo=react)](https://react.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%2B-336791.svg?style=flat-square&logo=postgresql)](https://www.postgresql.org/)

<br/>

<img src="docs/screenshots/home.jpg" alt="云途首页：重庆行程规划入口" width="100%" />

<p>
  <img src="docs/screenshots/plan.jpg" alt="云途行程详情：重庆三日街区漫步" width="100%" />
</p>

</div>

---

## 🌟 为什么选择 Yuntu？

市面上的大多数“AI 旅行规划”应用往往存在致命硬伤：
- **地点幻觉**：编造已经倒闭或根本不存在的餐厅、打卡点。
- **路线荒谬**：同一天内让游客在城市东西两头来回折返 4 次。
- **价格与时间瞎编**：随口捏造门票价格和游览耗时。

**Yuntu 采用“确定性核心 + 创造性外围”的生产级架构**：
- **地点与商圈**：纯 SQL 检索真实经过验证的经纬度数据，**绝不交由 LLM 凭空生成地点**。
- **路线与通勤**：基于地理空间算法与高德路径规划，严格控制单日通勤预算与同区聚类；内置路线可行性（Route Feasibility）硬约束。
- **住宿推荐**：基于到每日 Top POI 的平均通勤距离最短算法，确定性推荐最优住宿商圈。
- **行前准备**：根据季节天气、城市特性和出行人群自动推导必备打包清单与避坑贴士。
- **文案与建议**：LLM 仅在“结构化事实约束（Structured Evidence Payload）”下编写润色，杜绝编造。
- **质量门禁（Publish Gate）**：确定性代码复核，严防酒店品牌编造、价格声明与路线越界。

---

## 🏗️ 核心架构流程

```text
用户请求 (自然语言 / 结构化表单)
  │
  ▼
[ 1. 意图解析 (Intent Parser · LLM) ]
  解析目的地、出行天数、偏好、节奏、避开项
  │
  ▼
[ 2. 地点召回 (Data Retrieval · 纯 SQL) ]
  严格从数据库检索已验证的真实地点池 (无 LLM)
  │
  ▼
[ 3. 路线规划与聚类 (Route Planning & Feasibility · 空间算法) ]
  同区聚类、每日通勤耗时预算、空间距离最短排程 (无 LLM)
  │
  ▼
[ 4. 住宿商圈推导 (Accommodation Resolver · 空间算法) ]
  根据路线 POI 重心推导最省通勤时间的住宿区域 (无 LLM)
  │
  ▼
[ 5. 行前准备推导 (Pre-trip Advice · 规则 + 事实载荷) ]
  结合城市、游玩主题与天气，推导打包必备项与游玩贴士
  │
  ▼
[ 6. 攻略生成 (Final Writer · 事实约束 LLM) ]
  严格基于授权的事实载荷生成生动的每日行程建议
  │
  ▼
[ 7. 发布门禁复核 (Publish Gate · 确定性规则) ]
  硬性校验路线合规性、事实真实性与价格规范
  │
  ▼
交付标准结果 (JSON / Markdown / PDF / 结构化数据)
```

---

## 📦 开源范围

想直接用完整能力，打开 **[https://kakarot8.com](https://kakarot8.com)**。那里是线上完整版：登录、更多城市、完整规划链路。

这个仓库是规划引擎和配套前端的**源码**，方便自建和二次开发，不是把线上站原样打包。自己跑之前需要准备：

- Python 3.11+、Node.js 20+、PostgreSQL 16+
- 大模型 API Key（Gemini / DeepSeek / 其他 OpenAI 兼容网关）
- 高德 Web 服务 Key（路线与地理；前端地图另需 JS Key）

当前开源版还要注意：

- **免登录即用**：开箱即用，打开前端页面无需注册登录即可直接规划。
- **单城官方数据示范（重庆）**：
  > [!NOTE]
  > **为什么首页目前只显示重庆？**
  > 本开源仓库的核心是展示**生产级 AI 旅行规划引擎的架构与全链路实现**（确定性排程 + 空间算法 + 真实 POI 事实约束）。为降低本地自建与部署门槛，本仓库种子数据库当前**仅内置了【重庆】全套 27+ 核心经过验证的地点与经纬度数据**。
  > 
  > 为避免用户因缺少其他城市 POI 数据库而导致规划失败，**前端首页（桌面旅行册 Spread 与移动端探索流）默认聚焦并仅展示重庆**。如需扩展其他城市（如成都、西安、北京、上海等），只需向本地数据库 `travel_canonical_place` 导入对应城市的真实地点，并在 `src/api/destinations.py` 中启用对应城市即可。如需体验 16+ 热门名城完整在线服务，可访问官方线上体验站 [kakarot8.com](https://kakarot8.com)。
- **安全提醒**：`docker-compose.yml` 里的 `yuntu` / `yuntupassword` 是本地演示口令，生产环境请务必修改。

---

## 🛠️ 本地运行

先复制环境变量并填入自己的 Key：

```bash
git clone https://github.com/Trunks820/Yuntu.git
cd Yuntu
cp .env.example .env
```

### 后端

```bash
pip install -r requirements.txt
python -m scripts.init_db
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 6666 --reload
```

### 前端

```bash
cd web
cp .env.example .env.development   # 按需补 VITE_AMAP_KEY
npm install
npm run dev                        # http://localhost:3000
```

仓库里有 `docker-compose.yml`，可作参考，但**没有保证一键就能跑通全栈**。自己部署时注意：默认 API 只放行本机 IP，容器网络下前端反代可能会 403；公网暴露前请自己收紧端口和鉴权。

---

## ⚙️ 模型与环境配置 (`.env`)

Yuntu 支持主流大模型（Google Gemini、DeepSeek、OpenAI 及任何 OpenAI 兼容的网关）。完整字段见 `.env.example`，最少需要：

```env
# 数据库连接（本地演示账号，请改成你自己的）
DATABASE_URL=postgresql+asyncpg://yuntu:yuntupassword@localhost:5432/yuntu_travel

# 方式 A：单 Key 快速启动（DeepSeek 官网或兼容网关）
DEEPSEEK_API_KEY=your_deepseek_api_key

# 方式 B：使用 Google Gemini
# GEMINI_API_KEY=your_gemini_api_key

# 方式 C：按角色混用不同模型 / 网关（可选）
# ROUTE_PLANNER_PROVIDER=gemini
# ROUTE_PLANNER_MODEL=gemini-2.5-flash
# ROUTE_PLANNER_API_KEY=your_gemini_key
# WRITER_PROVIDER=deepseek
# WRITER_MODEL=deepseek-chat
# WRITER_API_KEY=your_deepseek_key

# 地理与路线服务 (高德 Web 服务 Key)
AMAP_API_KEY=your_amap_api_key
AMAP_ROUTE_ENABLED=true
```

---

## 📡 核心 API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/trip/async` | **主要生产入口**：提交异步旅行规划任务，返回 `job_id` |
| `GET` | `/trip/jobs/{job_id}` | 轮询任务状态（支持排队、阶段进度透出） |
| `GET` | `/trip/jobs/{job_id}/stream` | SSE 流式实时推送规划进度与最终结果 |
| `GET` | `/trip/results/{result_id}` | 获取完整结构化规划详情（含 POI 经纬度、交通、游玩耗时） |
| `POST` | `/trip` | 早期同步调试入口（遗留）。会直接打模型，自己暴露到公网时注意额度 |

---

## 📂 项目结构

```text
Yuntu/
├── web/                     # 🎨 现代前端界面 (React 18 + TailwindCSS + Vite + Lucide)
│   ├── src/                 # 桌面旅行册（Spread）四步启程 / 装订等待 / 双页路书 / 浏览器本地我的行程 + 经典胶囊与山河地图入口
│   └── Dockerfile           # 前端 Nginx 容器化镜像
├── src/                     # ⚙️ 核心规划引擎与 API
│   ├── agents/              # 意图理解、地点召回、排程与路线可行性、行前准备、文案生成、发布门禁
│   ├── api/                 # FastAPI 路由与公共接口
│   ├── cost_estimate/       # 费用估算引擎
│   ├── cost_reference/      # 消费与门票参考数据
│   ├── db/                  # 数据库权威模型与元数据
│   ├── export/              # PDF 与长图渲染引擎及城市相册
│   └── jobs/                # 异步任务调度与 Worker
├── sql/
│   ├── schema.sql           # PostgreSQL 全量建表脚本
│   └── seed_chongqing.sql   # 重庆精选真实 POI 与商圈种子数据
├── scripts/
│   ├── init_db.py           # 一键初始化数据库与种子
│   ├── trip.py              # 命令行单次端到端规划体验
│   └── stress_test.py       # 规划引擎回归与压测
├── docs/                    # 架构文档与 README 截图
├── docker-compose.yml       # 本地编排参考（Postgres + Backend + Frontend）
├── Dockerfile               # 后端 Python 3.11 镜像
├── LICENSE                  # MIT 开源协议
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
