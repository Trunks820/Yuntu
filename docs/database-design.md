# 数据库设计

PostgreSQL 数据库。基础内容、canonical place、异步任务、路线缓存、反馈、
城市领域和导出制品按版本演进；实际表/约束必须以 canonical SQL 与 live
schema audit 的交集为准，不能依赖历史表数量描述。

SQL 建表脚本见 `yuntu-travel-schema.sql`。

本文中标为 MVP / Phase 2 的“先存不用”“后续启用”等句子保留历史设计语境，
不能覆盖当前 v0.9.5 代码。当前列、约束和枚举必须以
`sql/yuntu-travel-schema.sql`、已执行迁移和 live schema audit 共同确认。

## 1. 四层架构

```
raw 层：原始可追溯
    travel_crawl_task      采集配置 / 关键词管理
    travel_crawl_run       v0.2 一次采集运行记录
    travel_crawl_run_step  v0.2 采集运行固定子步骤记录
    travel_raw_item        原始数据存储
    travel_extract_log     LLM 提纯记录

core 层：清洗建模
    travel_content         清洗后内容
    travel_place           标准化地点
    travel_place_source_map 跨平台映射
    travel_content_place_mention 内容↔地点关系
    travel_place_fact      地点事实

summary 层：给 hermes 快查
    travel_place_summary   聚合摘要

record 层：生成记录
    travel_trip_job        v0.2 旅行规划异步任务状态
    travel_plan_record     攻略生成记录
    travel_plan_feedback   v0.4 用户显式正负反馈

city domain 层：v0.5 canonical city 与质量巡检
    travel_city                    canonical city 与业务可用状态
    travel_city_alias              管理员维护的唯一 alias
    travel_city_demand             source + request_id 幂等需求
    travel_city_quality_snapshot   确定性质量巡检快照
    travel_city_keyword_review     待管理员审核扩展关键词
    travel_city_extension_keyword  已批准长期扩展关键词
    travel_city_crawl_batch        城市级串行采集批次
    travel_city_crawl_batch_item   城市批次内的关键词明细

route infra 层：路线参考与最终 leg cache
    travel_amap_district    Amap 行政区/adcode/citycode 参考
    travel_amap_route_cache Amap 最终通勤段缓存，v0.8.11 按 mode 隔离
```

## 2. 数据流

```
travel_crawl_task
    ↓ 创建一次运行
travel_crawl_run
    ↓ 固定子步骤：CRAWL / EXTRACT / REFRESH_SUMMARY
travel_crawl_run_step
    ↓ 采集器执行
travel_raw_item（原样保存）
    ↓ LLM 提纯
travel_extract_log（记录提纯过程）
    ↓ 结构化入库
travel_content + travel_place + mention + fact
    ↓ 聚合刷新
travel_place_summary
    ↓ hermes 查询生成攻略
travel_trip_job（v0.2 异步状态）
    ↓ 成功时关联
travel_plan_record（保存生成结果）
    ↓ 当前成功攻略可接受显式评价
travel_plan_feedback（v0.4 用户反馈事件）
```

## 3. 表详细设计

### 3.1 travel_crawl_task

管理低频采集配置和关键词。它不是一次采集运行记录。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| city | VARCHAR(50) | 采集城市 |
| keyword | VARCHAR(200) | 采集关键词 |
| source_platform | VARCHAR(20) | xhs / amap / dianping / ctrip / mafengwo / manual |
| source_type | VARCHAR(20) | note / poi / restaurant / review / guide / ranking / manual_route |
| crawl_frequency | VARCHAR(10) | 3d / 7d / 15d / 30d |
| status | VARCHAR(20) | ENABLED / DISABLED / RUNNING / PAUSED |
| last_result_status | VARCHAR(20) | SUCCESS / FAILED / LOGIN_EXPIRED / RISK_BLOCKED / NO_RESULT / API_CHANGED |
| priority | SMALLINT | 优先级 |
| next_run_time | TIMESTAMPTZ | 下次执行时间 |
| last_run_time | TIMESTAMPTZ | 上次执行时间 |
| last_success_time | TIMESTAMPTZ | 上次成功时间 |
| fail_count | INT | 连续失败次数 |
| fail_reason | TEXT | 失败原因 |
| created_time | TIMESTAMPTZ | 创建时间 |
| updated_time | TIMESTAMPTZ | 自动更新 |

唯一约束：`(city, keyword, source_platform, source_type)`

### 3.1.1 travel_crawl_run（v0.2）

记录一次内部采集运行父任务。外层 Hermes 通过内部接口触发采集时，`yuntu-travel` 创建本表记录和固定子步骤记录，并在后台执行 crawl / extract / refresh summary。

不要把一次运行状态塞进 `travel_crawl_task`。`travel_crawl_task` 继续表示可复用的采集配置；`travel_crawl_run` 表示一次执行。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键，对外作为 run_id 返回 |
| platform | VARCHAR(32) | 当前主要是 xhs |
| city | VARCHAR(64) | 采集城市 |
| keyword | VARCHAR(255) | 小红书搜索关键词 |
| limit_count | INT | 本次计划采集数量 |
| trigger_source | VARCHAR(32) | manual / hermes / cron，v0.2 主要使用 manual 和 hermes |
| status | VARCHAR(32) | PENDING / RUNNING / SUCCESS / FAILED / COOKIE_EXPIRED / PARTIAL_SUCCESS / TIMEOUT |
| run_extract | BOOLEAN | crawl 后是否执行提纯 |
| refresh_summary | BOOLEAN | 提纯后是否刷新摘要 |
| raw_count | INT | 原始采集数量 |
| insert_count | INT | 新增入库数量 |
| duplicate_count | INT | 重复数量 |
| failed_count | INT | 失败数量 |
| error_code | VARCHAR(64) | 安全错误码 |
| error_message | TEXT | 安全错误摘要，不保存 Cookie 或堆栈 |
| stdout_summary | TEXT | stdout 摘要，禁止包含 Cookie |
| stderr_summary | TEXT | stderr 摘要，禁止包含 Cookie |
| started_time | TIMESTAMPTZ | 开始时间 |
| finished_time | TIMESTAMPTZ | 结束时间 |
| created_time | TIMESTAMPTZ | 创建时间 |
| updated_time | TIMESTAMPTZ | 自动更新 |

### 3.1.2 travel_crawl_run_step（v0.2）

记录一次采集运行的固定子步骤状态。v0.2 只允许固定顺序：`CRAWL -> EXTRACT? -> REFRESH_SUMMARY?`，不做通用 DAG 或任意步骤编排。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| run_id | BIGINT FK | 关联 travel_crawl_run |
| step_name | VARCHAR(32) | CRAWL / EXTRACT / REFRESH_SUMMARY |
| status | VARCHAR(32) | PENDING / RUNNING / SUCCESS / FAILED / SKIPPED / TIMEOUT |
| started_time | TIMESTAMPTZ | 开始时间 |
| finished_time | TIMESTAMPTZ | 结束时间 |
| raw_count | INT | 原始采集数量 |
| insert_count | INT | 新增入库数量 |
| duplicate_count | INT | 重复数量 |
| failed_count | INT | 失败数量 |
| error_code | VARCHAR(64) | 安全错误码 |
| error_message | TEXT | 安全错误摘要 |
| stdout_summary | TEXT | stdout 摘要，禁止包含 Cookie |
| stderr_summary | TEXT | stderr 摘要，禁止包含 Cookie |
| created_time | TIMESTAMPTZ | 创建时间 |
| updated_time | TIMESTAMPTZ | 自动更新 |

唯一约束：`(run_id, step_name)`

### 3.2 travel_raw_item

所有来源的原始采集数据。不追求字段优雅，只追求可追溯、可回放、可重新解析。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| source_platform | VARCHAR(20) | 来源平台 |
| source_type | VARCHAR(20) | 内容类型 |
| source_id | VARCHAR(200) | 来源平台原始 ID |
| source_url | TEXT | 来源链接 |
| city | VARCHAR(50) | 采集城市 |
| keyword | VARCHAR(200) | 采集关键词 |
| request_params | JSONB | 本次请求参数 |
| raw_json | JSONB | 接口原始 JSON |
| raw_text | TEXT | 提取出的可读文本 |
| content_hash | VARCHAR(64) | 内容 hash，去重用 |
| crawl_task_id | BIGINT FK | 关联 crawl_task |
| crawl_time | TIMESTAMPTZ | 采集时间 |
| schema_version | VARCHAR(10) | 解析版本号 |
| raw_status | VARCHAR(20) | NORMAL / DELETED / PRIVATE / BLOCKED / EMPTY |
| parse_status | VARCHAR(20) | PENDING / PARSED / FAILED / IGNORED |
| parse_error | TEXT | 解析失败原因 |
| created_time | TIMESTAMPTZ | 入库时间 |

唯一约束：
- `(source_platform, source_type, source_id)`
- `(source_platform, content_hash)`

### 3.3 travel_extract_log

LLM 提纯记录。追踪提纯质量、对比 Prompt 版本效果、控制 token 成本。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| raw_item_id | BIGINT FK | 关联 raw_item |
| extract_type | VARCHAR(30) | content_summary / place_mention / place_fact / route_extract / full_extract / poi_normalize / review_summary |
| extract_model | VARCHAR(50) | gpt-4o / claude-sonnet / deepseek-v3 |
| extract_prompt_version | VARCHAR(50) | Prompt 版本号 |
| input_hash | VARCHAR(64) | 输入文本 hash，判断是否需要重跑 |
| extract_input_text | TEXT | 喂给 LLM 的完整文本 |
| extract_output_raw | TEXT | LLM 原始返回 |
| extract_output_json | JSONB | 解析后的结构化结果 |
| extract_status | VARCHAR(20) | PENDING / SUCCESS / FAILED / PARTIAL |
| extract_error | TEXT | 失败原因 |
| token_input | INT | 输入 token |
| token_output | INT | 输出 token |
| latency_ms | INT | 提纯耗时 |
| review_status | VARCHAR(20) | UNREVIEWED / APPROVED / REJECTED / NEEDS_FIX |
| review_note | TEXT | 人工复盘备注 |
| created_time | TIMESTAMPTZ | 创建时间 |
| updated_time | TIMESTAMPTZ | 自动更新 |

同一个 raw_item 可被多次提纯（换模型、换 Prompt）。

### 3.4 travel_content

清洗后的内容材料，Data Retrieval Agent 查询入口。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| raw_item_id | BIGINT FK | 关联 raw_item |
| extract_log_id | BIGINT FK | 关联 extract_log（人工录入可为空） |
| city | VARCHAR(50) | 城市 |
| source_platform | VARCHAR(20) | 来源平台 |
| source_type | VARCHAR(20) | note / guide / review / ranking / manual_route |
| source_id | VARCHAR(200) | 来源平台原始 ID |
| source_url | TEXT | 来源链接 |
| title | VARCHAR(500) | 标题 |
| author | VARCHAR(200) | 作者 |
| content_text | TEXT | 正文 |
| content_summary | TEXT | 摘要 |
| tags | JSONB | 业务标签 `["美食", "citywalk"]` |
| content_type_tags | JSONB | 内容形态标签 `["攻略", "路线"]` |
| quality_score | NUMERIC(3,1) | 内容质量评分 0-10 |
| hot_score | NUMERIC(10,2) | 综合热度 |
| language | VARCHAR(5) | zh / en |
| publish_time | TIMESTAMPTZ | 内容发布时间 |
| captured_time | TIMESTAMPTZ | 采集时间 |
| created_time | TIMESTAMPTZ | 创建时间 |
| updated_time | TIMESTAMPTZ | 自动更新 |

GIN 索引：`tags`、`content_type_tags`

### 3.5 travel_place

标准化地点 / POI 核心表。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| city | VARCHAR(50) | 城市 |
| name | VARCHAR(200) | 地点名称 |
| normalized_name | VARCHAR(200) | 标准化名称（去重匹配用） |
| place_type | VARCHAR(30) | attraction / restaurant / business_area / market / park / museum / photo_spot / hotel / other |
| address | VARCHAR(500) | 地址 |
| longitude | NUMERIC(10,7) | 经度 |
| latitude | NUMERIC(10,7) | 纬度 |
| tags | JSONB | 标签 `["拍照", "夜景"]` |
| alias_names | JSONB | 别名 `["橘子洲景区", "橘子洲头"]` |
| created_time | TIMESTAMPTZ | 创建时间 |
| updated_time | TIMESTAMPTZ | 自动更新 |

模糊搜索索引：`pg_trgm` on name / normalized_name

### 3.6 travel_place_source_map

同一地点在不同平台的映射。**MVP 阶段跳过**（只有 xhs 一个来源）。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| place_id | BIGINT FK | 关联 place |
| source_platform | VARCHAR(20) | 来源平台 |
| source_id | VARCHAR(200) | 来源平台地点 ID |
| source_name | VARCHAR(200) | 来源平台里的地点名称 |
| source_url | TEXT | 来源链接 |
| confidence | NUMERIC(3,2) | 映射置信度 0-1 |
| created_time | TIMESTAMPTZ | 创建时间 |
| updated_time | TIMESTAMPTZ | 自动更新 |

唯一约束：`(source_platform, source_id)`

### 3.7 travel_content_place_mention

内容与地点的关系表。聚合 summary 的核心数据源。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| content_id | BIGINT FK | 关联 content |
| place_id | BIGINT FK | 关联 place |
| place_name_text | VARCHAR(200) | 原文中的地点名称 |
| mention_context | TEXT | 提及上下文 |
| mention_type | VARCHAR(20) | recommend / avoid / pass_by / food / photo / stay / transport |
| sentiment | VARCHAR(10) | positive / neutral / negative |
| tags | JSONB | 提及标签 |
| route_order | SMALLINT | 路线顺序（如有） |
| confidence | NUMERIC(3,2) | 匹配置信度 |
| created_time | TIMESTAMPTZ | 创建时间 |

### 3.8 travel_place_fact

地点事实，来自不同数据源。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| place_id | BIGINT FK | 关联 place |
| source_platform | VARCHAR(20) | 来源平台 |
| source_id | VARCHAR(200) | 来源原始 ID |
| source_url | TEXT | 来源链接 |
| fact_type | VARCHAR(30) | rating / avg_price / opening_hours / recommendation / warning / visit_duration / best_time / crowd_level / traffic_tip / photo_tip / food_recommendation |
| fact_value | JSONB | 事实内容 |
| confidence | NUMERIC(3,2) | 置信度 |
| captured_time | TIMESTAMPTZ | 采集时间 |
| created_time | TIMESTAMPTZ | 创建时间 |

MVP 阶段只使用小红书可直接提供的 fact_type：`recommendation` / `warning` / `visit_duration` / `traffic_tip` / `photo_tip` / `food_recommendation`。`rating` / `opening_hours` / `avg_price` 等需高德或点评数据，Phase 2 再启用。

### 3.9 travel_place_summary

聚合摘要表，hermes 查询核心入口。由刷新任务维护，非实时 view。

| 字段 | 类型 | 说明 |
|------|------|------|
| place_id | BIGINT PK FK | 主键，关联 place |
| city | VARCHAR(50) | 城市 |
| name | VARCHAR(200) | 地点名称 |
| place_type | VARCHAR(30) | 地点类型 |
| address | VARCHAR(500) | 地址 |
| longitude | NUMERIC(10,7) | 经度 |
| latitude | NUMERIC(10,7) | 纬度 |
| tags | JSONB | 聚合标签 |
| mention_count_30d | INT | 近 30 天被提及次数 |
| positive_count_30d | INT | 近 30 天正向提及次数 |
| negative_count_30d | INT | 近 30 天负向提及次数 |
| source_count | INT | 覆盖来源数量 |
| latest_captured_time | TIMESTAMPTZ | 最近采集时间 |
| rating | NUMERIC(3,1) | 聚合评分 |
| avg_price | NUMERIC(8,2) | 聚合人均 |
| opening_hours | VARCHAR(200) | 营业时间 |
| top_reasons | JSONB | 推荐理由列表 |
| cleaned_evidence | JSONB | 离线清洗后的推荐理由（优先于 top_reasons） |
| warnings | JSONB | 避坑提醒列表 |
| hot_score | NUMERIC(10,2) | 热度分 |
| quality_score | NUMERIC(3,1) | 数据质量分 0-10 |
| recommend_score | NUMERIC(3,1) | 综合推荐分 0-10 |
| updated_time | TIMESTAMPTZ | 聚合更新时间 |

### 3.10 travel_plan_record

攻略生成记录，复盘质量和优化 prompt 的依据。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| user_query | TEXT | 用户原始输入 |
| from_city | VARCHAR(50) | 出发城市 |
| to_city | VARCHAR(50) | 目的地 |
| start_date | DATE | 出发日期（先存不用，Phase 2 启用） |
| end_date | DATE | 返回日期（先存不用，Phase 2 启用） |
| days | SMALLINT | 天数 |
| nights | SMALLINT | 晚数，由 days 派生，用于兼容当前落库记录 |
| people_count | SMALLINT | 人数 |
| preferences | JSONB | 偏好 `["美食", "citywalk"]` |
| avoid | JSONB | 排除项 `["人流量过多", "打卡式旅游"]` |
| notes | TEXT | 用户补充说明 |
| used_content_ids | JSONB | 引用的 content ID 列表 |
| used_place_ids | JSONB | 引用的 place ID 列表 |
| used_fact_ids | JSONB | 引用的 fact ID 列表 |
| generated_plan | TEXT | 生成的攻略文本 |
| plan_json | JSONB | 结构化攻略 |
| prompt_text | TEXT | 核心 prompt |
| model_name | VARCHAR(50) | 使用的模型 |
| quality_feedback | TEXT | YunTu Review 审核意见，不用于保存用户反馈 |
| quality_metrics | JSONB | 内部质量/验收指标；只有公共合同明确列出的白名单投影可进入 `/trip/results`，v0.8.11 包括 `commute_mode_report` |
| created_time | TIMESTAMPTZ | 生成时间 |

### 3.11 travel_plan_feedback（v0.4）

记录用户对当前会话最近一份成功攻略的显式正负反馈，供人工复盘。反馈不自动修改排序、prompt 或采集策略，也不复用 `travel_plan_record.quality_feedback`。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 主键 |
| request_id | VARCHAR(512) | 入口事件幂等键，唯一 |
| result_record_id | BIGINT FK | 被评价的 `travel_plan_record` |
| source | VARCHAR(30) | 来源，例如 `wecom_kf` |
| conversation_id | VARCHAR(200) | 真实会话标识 |
| rating | VARCHAR(20) | positive / negative |
| feedback_text | TEXT | 用户明确反馈正文 |
| created_time | TIMESTAMPTZ | 创建时间 |

## 4. MVP 使用优先级

| 表 | 级别 | 说明 |
|----|------|------|
| travel_raw_item | 重度 | 所有原始数据必进 |
| travel_extract_log | 重度 | prompt 调优和成本追踪 |
| travel_content | 重度 | Data Retrieval 查询用 |
| travel_place | 重度 | 地点主表 |
| travel_content_place_mention | 重度 | 聚合 summary 核心 |
| travel_place_summary | 重度 | hermes 查询入口 |
| travel_plan_record | 重度 | 记录生成结果 |
| travel_export_artifact | 轻度 | v0.8.10.1 后端导出制品 metadata；binary 文件不入库 |
| travel_export_quota | 轻度 | v0.8.10.1 每 IP 每日导出生成强限制 |
| travel_crawl_task | 轻度 | 记录关键词，不驱动调度 |
| travel_place_fact | 轻度 | 先只存 recommendation / warning / visit_duration / traffic_tip / photo_tip / food_recommendation；rating / opening_hours / avg_price 等高德或点评数据稳定后再启用 |
| travel_place_source_map | 跳过 | MVP 暂不做跨平台 POI 对齐 |

## 5. v0.2 稳定化扩展表

0.2 新增两类运行状态表，分别服务不同边界。

旅行规划异步任务状态新增独立表，不塞进 `travel_plan_record`。`travel_plan_record` 继续只表示已生成的攻略结果；异步请求、执行状态、失败原因和最终结果关联由 `travel_trip_job` 负责。

- **travel_trip_job**：job_id、request_id、source、conversation_id、user_display_name、user_query、status、current_stage、result_record_id、reply_text、error_message、created_time、started_time、finished_time、updated_time
- **travel_crawl_run**：run_id、platform、city、keyword、limit_count、status、run_extract、refresh_summary、raw_count、insert_count、duplicate_count、failed_count、error_code、error_message、created_time、started_time、finished_time、updated_time
- **travel_crawl_run_step**：run_id、step_name、status、raw_count、insert_count、duplicate_count、failed_count、error_code、error_message、started_time、finished_time、updated_time

`travel_trip_job.user_display_name` 是入口侧传入的可选用户昵称，只用于灰度测试复盘、人工排查和用户管理；它不参与攻略生成、不传给 Writer / Review，也不随 `/trip/jobs/{job_id}` 状态响应返回。

## 6. v0.8.10.1 后端导出制品表

v0.8.10.1 新增 backend export artifacts，用于点击触发的文本 PDF 和分享海报。它不改变
`travel_plan_record` 的含义：`travel_plan_record` 仍表示已发布攻略结果；导出制品只是从
`PLAN_READY` 结果派生出的文件。

Binary 文件不进入 DB。首发存本地文件系统，保留 7 天；DB 只保存 metadata，并通过
`storage_backend` / `storage_key` 为后续对象存储迁移预留合同。

### 6.1 travel_export_artifact

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 内部主键 |
| artifact_id | VARCHAR(32) UNIQUE | 对外 artifact 标识 |
| result_record_id | BIGINT FK | 关联 `travel_plan_record(id)` |
| artifact_type | VARCHAR(20) | `pdf` / `share_image` |
| status | VARCHAR(20) | `pending` / `running` / `ready` / `failed` |
| source_hash | VARCHAR(64) | 当前 export source 的稳定 hash |
| export_version | VARCHAR(20) | 导出渲染/合同版本 |
| storage_backend | VARCHAR(20) | 首发 `local`，后续可迁 `r2` |
| storage_key | TEXT | 内部文件 key/path，不对前端公开 |
| filename | TEXT | 下载文件名 |
| mime_type | VARCHAR(100) | `application/pdf` / `image/png` |
| byte_size | BIGINT | 文件大小 |
| sha256 | VARCHAR(64) | 文件 hash |
| text_length | INT | PDF 文本长度；图片为空 |
| width_px | INT | 图片宽度；PDF 为空 |
| height_px | INT | 图片高度；PDF 为空 |
| page_count | INT | PDF 页数；图片为空 |
| metadata | JSONB | 小型渲染元数据，如 cover/background fallback 状态 |
| attempt_count | INT | worker 尝试次数 |
| error_code | VARCHAR(50) | 稳定失败码 |
| error_message | TEXT | 安全错误说明 |
| client_ip_hash | VARCHAR(64) | 创建请求 IP hash，不存 raw IP |
| created_time | TIMESTAMPTZ | 创建时间 |
| started_time | TIMESTAMPTZ | 最近一次开始时间 |
| finished_time | TIMESTAMPTZ | ready/failed 时间 |
| expires_time | TIMESTAMPTZ | 文件过期时间 |
| updated_time | TIMESTAMPTZ | 更新时间 |

唯一约束：

```text
UNIQUE (result_record_id, artifact_type, source_hash, export_version)
```

索引：

```text
(result_record_id, artifact_type)
(status, created_time)
(expires_time)
```

### 6.2 travel_export_quota

按 client IP hash、artifact type 和 Asia/Shanghai 业务日执行强限制：

```text
pdf:         5 new generations / day
share_image: 100 new generations / day
```

`share_image=100` 是首轮 50 账号公测的全站/IP 事故熔断阈值，不是普通用户
额度。普通用户仍由 yuntu-bff 的 3 次成功攻略额度约束；本轮不新增独立的
每用户生图次数表，也不重置已有 quota row。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | BIGSERIAL PK | 内部主键 |
| quota_day | DATE | Asia/Shanghai 业务日 |
| client_ip_hash | VARCHAR(64) | 请求 IP hash，不存 raw IP |
| artifact_type | VARCHAR(20) | `pdf` / `share_image` |
| generate_count | INT | 当日新生成次数 |
| created_time | TIMESTAMPTZ | 创建时间 |
| updated_time | TIMESTAMPTZ | 更新时间 |

唯一约束：

```text
UNIQUE (quota_day, client_ip_hash, artifact_type)
```

Redis 不作为 quota source of truth。缓存命中、重复下载、状态轮询不计数；新建 artifact
和用户主动重试 failed artifact 计数。

## 7. v0.8.11 Commute Mode 数据合同

v0.8.11 不新增结果主表，也不把结果合同塞进 `travel_trip_job`。请求模式、
generation base mode、最终 leg effective mode 与 duration source 的持久化
边界如下：

- `travel_trip_job.trip_request_json.commute_mode` 保存异步结构化请求的
  requested mode，flag off 也不得改写；
- 最终 route legs 随 `travel_plan_record.plan_json[].route_plan` 持久化，
  `mode` 是 effective mode，内部 `source` 是 `amap | estimate`；
- v0.8.11.1 的 `transit_steps` 继续随对应 `CommuteLeg` 写入
  同一个 `plan_json[].route_plan`；`travel_plan_record` 不新增列或表；
- `transit_summary` 由 steps 确定性重建，不作为第二份结构化事实持久化；
- `travel_plan_record.quality_metrics.commute_mode_report` 保存 requested、
  report-level effective/effective_reason、最终降级/理性化数量和聚合
  duration source；
- `/trip/results` 从 `travel_plan_record.plan_json + quality_metrics` 投影
  schema 1.3。它不读取 `travel_trip_job` 作为结果源；
- 旧 record 无 commute report 时投影
  `request.commute_mode="driving"`，report 可缺失。

v0.8.11.1 将响应投影统一升级到 schema 1.4：旧记录仍从上述
字段兼容读取，不回填、不改写，也不额外保存 record-level schema version。
新旧记录在部署后都返回当前 1.4 projection；旧 transit leg 没有 steps 时投影
空数组并进入明细缺失降级，历史 walking 保持原值。

### 7.1 travel_amap_route_cache mode isolation

v0.8.11 为现有最终-leg cache 增加 mode 维度：

| 字段 | 类型 | 说明 |
|------|------|------|
| provider | VARCHAR(20) | legacy v3 driving 使用 `amap`；v0.8.11 enabled v5 cache 使用 `amap_v5`，隔离 API generation |
| strategy | INT | 当前策略编号 |
| origin_place_id | BIGINT | 有向起点 place id |
| destination_place_id | BIGINT | 有向终点 place id |
| mode | VARCHAR(10) NOT NULL DEFAULT `driving` | `driving` / `transit` / `walking` / `cycling`，等于 leg effective mode |
| origin_longitude / origin_latitude | DOUBLE PRECISION | 写缓存时的起点坐标指纹 |
| destination_longitude / destination_latitude | DOUBLE PRECISION | 写缓存时的终点坐标指纹 |
| distance_meters | INT | 对应 mode 的路线距离 |
| duration_minutes | INT | 对应 mode 的路线耗时 |
| transit_steps_json | JSONB NULL | v0.8.11.1 provider steps；不保存派生 `transit_summary`；受保护 migration 已执行两次并验证 nullable JSONB，39 条 district metadata 已同步且固定五城 citycode 已验证；当前 v0.9.5 生产通勤开关已启用（2026-08-10 只读确认），原修订版五城 live smoke 证据仍未补录 |
| hit_count / last_hit_time | INT / TIMESTAMPTZ | 命中观测 |
| created_time / updated_time | TIMESTAMPTZ | 生命周期 |

唯一约束使用明确名称：

```text
CONSTRAINT uq_travel_amap_route_cache_provider_strategy_pair_mode
UNIQUE (provider, strategy, origin_place_id, destination_place_id, mode)
```

Memory、Redis 与 Postgres preload/lookup/store 都必须显式携带 effective
mode 和 API-generation namespace。不得依赖 default driving；default
只用于旧行迁移兼容。同一有向 place pair 的四种 mode 可以同时存在且不得
串读；同一 driving pair 的 legacy v3 `amap` 与 enabled v5 `amap_v5` 也不得
串读。

v0.8.11.1 公共交通明细使用独立的
`provider='amap_v5_transit_v2'` namespace，并在 Memory/Redis key 中加入
`transit_detail_v2`。只有显式版本 2 且结构合法的 steps payload 才算明细
cache hit；`amap_v5` totals-only 与 `amap_v5_transit_v1` 旧行都视为 miss，
既不删除也不回填。TTL 继续
使用现有 `amap_route_cache_ttl_hours=72`。回滚代码仍读取 `amap_v5`，不会与
新 namespace 串读。该版本只在现有表增加 nullable JSONB 列，不创建
新表。

### 7.2 travel_amap_district citycode

Amap v5 transit 要求 `city1`、`city2` 使用 citycode，不能传 city name 或
adcode。现有 `travel_amap_district` 增加 nullable `citycode TEXT`，由已有
district sync 从官方 Amap response 写入。运行时每个 job 解析一次 citycode，
不按 leg 调用行政区 API。

生产开启 `COMMUTE_MODE_ENABLED` 前，目标 ACTIVE/GRAY 城市必须完成所需
citycode 覆盖。缺失 citycode 的 transit leg 走可观测的 same-effective-mode
estimate degradation，不得构造错误参数或静默改用 driving。

### 7.3 Migration gate and repeatability

实现 `sql/v0.8.11-commute-mode-cache.sql` 前必须先只读审计 live schema：

- `travel_amap_route_cache` 当前 columns/defaults/indexes；
- `pg_constraint` 中旧四列 unique constraint 的真实名称与列集合；
- `travel_amap_district` 是否存在及 citycode 列/覆盖；
- 是否存在与新五列 unique key 冲突的数据。

迁移合同：

1. `ADD COLUMN IF NOT EXISTS mode ... DEFAULT 'driving'`，既有行保持可读；
2. 通过 `pg_catalog` 识别并只删除旧四列 unique constraint，不猜生成名；
3. 通过 guarded `DO` block 增加明确命名的五列 unique constraint；
4. `ADD COLUMN IF NOT EXISTS citycode TEXT`；
5. canonical `sql/yuntu-travel-schema.sql` 同步 mode/citycode/constraint；
6. 重复执行迁移为 no-op，不删除 cache rows、不改变 existing driving data。

## 8. 后续扩展表（Phase 2）

### 8.1 v0.3 数据质量增量

`travel_plan_record` 增加 `quality_metrics JSONB`，保存成功攻略的内部质量指标，
例如合格候选数、单次回答内重合率、同会话上一轮重合率和多样性缺口。
该字段默认不进入公共旅行规划接口响应；只有版本化 API 合同明确列出的
白名单子结构可以投影。异步执行状态仍由 `travel_trip_job` 管理。
已有数据库使用 `sql/v0.3-data-quality.sql` 幂等升级。

### 8.2 v0.4 产品体验增量

`travel_trip_job.request_id` 扩展到 `VARCHAR(512)`，`travel_trip_job.conversation_id` 扩展到 `VARCHAR(200)`，支持由真实企微会话标识组成的入口事件幂等键。新增 `travel_plan_feedback` 保存当前攻略的显式正负反馈事件。

已有数据库使用 `sql/v0.4-product-experience.sql` 幂等升级。

### 8.3 v0.5 城市领域增量

已有数据库使用 `sql/v0.5-multi-city.sql` 幂等升级。v0.5 建立城市身份、需求热度、确定性质量快照、城市采集批次、攻略 city gate 和内部城市监督接口。

- **travel_city**：以唯一 `canonical_name` 作为稳定城市 key；`status` 只允许 `DISCOVERED / GRAY / ACTIVE / DISABLED`。采集活动不是城市状态。
- **travel_city_alias**：管理员维护的 alternate name；`alias` 全局唯一，只能映射到一个 canonical city。canonical name 本身作为隐式 alias。canonical 与 alias 写入按名称使用事务级 advisory lock 串行化，防止跨表并发冲突。
- **travel_city_demand**：保存完整攻略请求，使用 `(source, request_id)` 唯一约束和事务级 advisory lock 抵御入口重放和 Hermes 重试。追问、同步调试和内部操作不写入。热度重算只能初始化或提前 `next_refresh_time`，不能因为新 demand、全量重算或重复迁移向后推迟既有截止时间；完成 refresh 后由后续调度阶段显式建立下一周期。
- **travel_city_quality_snapshot**：保存 SQL 和确定性规则得到的有效地点数、有效证据数、四类覆盖、成功基础关键词、阻断问题以及 `GRAY / ACTIVE` eligible 标记。v0.5.2 尚无运行态 blocker 来源，因此 `blocking_issues` 初始化为空；后续只允许新增确定性 blocker code，不能引入 LLM 裁判。
- **travel_city_keyword_review**：保存待管理员审核的扩展关键词候选。
- **travel_city_extension_keyword**：保存已批准的长期扩展关键词；单城市最多启用 10 个。
- **travel_city_crawl_batch**：保存城市级串行采集批次状态；首版全局最多一个 `PENDING / RUNNING` 批次。
- **travel_city_crawl_batch_item**：保存批次内关键词明细，每个 item 关联一个底层 `travel_crawl_run`。

迁移会从 `travel_raw_item.city`、`travel_place_summary.city` 和 `travel_plan_record.to_city` 提取 distinct city，以现有字符串初始化 canonical name，并执行一次确定性质量巡检。达到 `GRAY` 的城市自动开放；达到 `ACTIVE` 门槛只记录 eligible，首次激活仍需管理员确认。

### 8.4 v0.5 Stage 2 攻略城市 gate

城市 gate 在 Intent Parser 之后执行，不改变 `POST /trip/async` 的快速排队合同。异步 worker 对不可用城市写入业务终止状态，不混入系统失败：

- `travel_trip_job.status += REJECTED`
- `travel_trip_job.current_stage += CITY_GATE_REJECTED`
- `travel_trip_job.error_code += CITY_PREPARING / CITY_COLLECTION_FAILED / CITY_DATA_INSUFFICIENT / CITY_DISABLED / CITY_CLARIFICATION_REQUIRED`

同步 `/trip` 复用相同 gate，但不写 `travel_city_demand`、不创建城市、不触发自动采集。Stage 2 只返回结构化拒绝状态；自动创建城市采集批次留到 Stage 4 和 Stage 6。

### 8.5 v0.5 Stage 3 scoped extract

`travel_raw_item.crawl_run_id` 可选关联到底层 `travel_crawl_run.id`，用于把自动采集产生的 raw item 限定到对应单关键词 run。旧数据允许为 `NULL`，人工补提纯不传 scope 时仍消费全局 `PENDING` raw item。

重复 `source_id` 不会被默认转移到新 run；只有旧 raw item 从非 `NORMAL` 恢复为 `NORMAL` 时，才会更新 `crawl_run_id` 并重新进入 `PENDING`，使批次指标只统计本次实际新增或恢复的数据。

### 8.6 v0.5 Stage 4 城市采集批次

`travel_city_crawl_batch` 表示一个城市级串行批次，首版通过唯一 partial index 保证全局最多一个 `PENDING / RUNNING` batch。`travel_city_crawl_batch_item` 表示批次内的一个搜索关键词，每个 item 最多关联一个底层 `travel_crawl_run`。

Stage 4 worker 只在后台执行 store 层创建出的 batch，不新增 internal city API 或 cron。worker 为每个关键词创建底层 crawl run 且只执行 crawl；所有关键词结束后统一执行 scoped extract、按 canonical city refresh summary，并执行一次确定性质量巡检。实际 refresh 完成时会更新 `last_refresh_time`，并按当前 30 天需求热度显式建立下一周期 `next_refresh_time`。

稳定后可增加 Agent 运行态记录：

- **travel_workflow**：workflow_id、user_query、trip_request_json、status、current_stage
- **travel_agent_run**：workflow_id、agent_name、status、input_json、output_json、latency_ms
