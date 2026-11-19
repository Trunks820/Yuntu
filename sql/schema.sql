-- ============================================================
-- yuntu-travel 数据库初始化脚本（兼容文件名保留）
-- 数据库：PostgreSQL
-- 版本：v1.2
-- ============================================================

-- -----------------------------------------------------------
-- 0. 扩展与通用函数
-- -----------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION fn_set_updated_time()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_time = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------
-- 1. travel_crawl_task  管理低频采集任务
-- -----------------------------------------------------------

CREATE TABLE travel_crawl_task (
    id              BIGSERIAL       PRIMARY KEY,
    city            VARCHAR(50)     NOT NULL,
    keyword         VARCHAR(200)    NOT NULL,
    source_platform VARCHAR(20)     NOT NULL
        CHECK (source_platform IN ('xhs', 'amap', 'dianping', 'ctrip', 'mafengwo', 'manual')),
    source_type     VARCHAR(20)     NOT NULL
        CHECK (source_type IN ('note', 'poi', 'restaurant', 'review', 'guide', 'ranking', 'manual_route')),
    crawl_frequency VARCHAR(10)     NOT NULL DEFAULT '7d'
        CHECK (crawl_frequency IN ('3d', '7d', '15d', '30d')),
    status          VARCHAR(20)     NOT NULL DEFAULT 'ENABLED'
        CHECK (status IN ('ENABLED', 'DISABLED', 'RUNNING', 'PAUSED')),
    last_result_status VARCHAR(20)
        CHECK (last_result_status IN ('SUCCESS', 'FAILED', 'LOGIN_EXPIRED', 'RISK_BLOCKED', 'NO_RESULT', 'API_CHANGED')),
    priority        SMALLINT        NOT NULL DEFAULT 0,
    next_run_time   TIMESTAMPTZ,
    last_run_time   TIMESTAMPTZ,
    last_success_time TIMESTAMPTZ,
    fail_count      INT             NOT NULL DEFAULT 0,
    fail_reason     TEXT,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uk_crawl_task_identity ON travel_crawl_task (city, keyword, source_platform, source_type);
CREATE INDEX idx_crawl_task_status_next ON travel_crawl_task (status, next_run_time);
CREATE INDEX idx_crawl_task_city ON travel_crawl_task (city);
CREATE INDEX idx_crawl_task_platform ON travel_crawl_task (source_platform);

CREATE TRIGGER trg_crawl_task_updated
    BEFORE UPDATE ON travel_crawl_task
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_crawl_task IS '低频采集任务管理表，yuntu-travel 巡检主要查询此表';


-- -----------------------------------------------------------
-- 2. travel_raw_item  原始采集数据
-- -----------------------------------------------------------

CREATE TABLE travel_raw_item (
    id              BIGSERIAL       PRIMARY KEY,
    source_platform VARCHAR(20)     NOT NULL
        CHECK (source_platform IN ('xhs', 'amap', 'dianping', 'ctrip', 'mafengwo', 'manual')),
    source_type     VARCHAR(20)     NOT NULL
        CHECK (source_type IN ('note', 'poi', 'restaurant', 'review', 'guide', 'ranking', 'manual_route')),
    source_id       VARCHAR(200),
    source_url      TEXT,
    city            VARCHAR(50),
    keyword         VARCHAR(200),
    request_params  JSONB,
    raw_json        JSONB,
    raw_text        TEXT,
    content_hash    VARCHAR(64),
    crawl_task_id   BIGINT          REFERENCES travel_crawl_task(id),
    crawl_run_id    BIGINT,
    crawl_time      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    schema_version  VARCHAR(10)     NOT NULL DEFAULT '1.0',
    raw_status      VARCHAR(20)     NOT NULL DEFAULT 'NORMAL'
        CHECK (raw_status IN ('NORMAL', 'DELETED', 'PRIVATE', 'BLOCKED', 'EMPTY')),
    parse_status    VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
        CHECK (parse_status IN ('PENDING', 'PARSED', 'FAILED', 'IGNORED')),
    parse_error     TEXT,
    provider        TEXT,
    provider_request_id TEXT,
    note_type       TEXT,
    author_id       TEXT,
    author_name     TEXT,
    author_url      TEXT,
    publish_time    TIMESTAMPTZ,
    image_urls      JSONB           NOT NULL DEFAULT '[]'::JSONB,
    video_urls      JSONB           NOT NULL DEFAULT '[]'::JSONB,
    cover_url       TEXT,
    liked_count     BIGINT,
    collected_count BIGINT,
    comment_count   BIGINT,
    shared_count    BIGINT,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uk_raw_item_source ON travel_raw_item (source_platform, source_type, source_id)
    WHERE source_id IS NOT NULL;
CREATE UNIQUE INDEX uk_raw_item_hash ON travel_raw_item (source_platform, content_hash)
    WHERE content_hash IS NOT NULL;
CREATE INDEX idx_raw_item_city_keyword ON travel_raw_item (city, keyword);
CREATE INDEX idx_raw_item_parse_status ON travel_raw_item (parse_status);
CREATE INDEX idx_raw_item_crawl_task ON travel_raw_item (crawl_task_id);
CREATE INDEX idx_raw_item_crawl_run ON travel_raw_item (crawl_run_id);
CREATE INDEX idx_raw_item_crawl_time ON travel_raw_item (crawl_time);

COMMENT ON TABLE travel_raw_item IS '所有来源的原始采集数据，不追求字段优雅，只追求可追溯、可回放、可重新解析';


-- -----------------------------------------------------------
-- 3. travel_extract_log  LLM 提纯记录（支持人工复盘）
-- -----------------------------------------------------------

CREATE TABLE travel_extract_log (
    id              BIGSERIAL       PRIMARY KEY,
    raw_item_id     BIGINT          NOT NULL REFERENCES travel_raw_item(id),
    extract_type    VARCHAR(30)     NOT NULL
        CHECK (extract_type IN (
            'content_summary', 'place_mention', 'place_fact', 'route_extract',
            'full_extract', 'poi_normalize', 'review_summary'
        )),
    extract_model   VARCHAR(50)     NOT NULL,
    extract_prompt_version VARCHAR(50) NOT NULL DEFAULT 'v1',
    input_hash      VARCHAR(64),
    extract_input_text TEXT         NOT NULL,
    extract_output_raw TEXT,
    extract_output_json JSONB,
    extract_status  VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
        CHECK (extract_status IN ('PENDING', 'SUCCESS', 'FAILED', 'PARTIAL')),
    extract_error   TEXT,
    token_input     INT,
    token_output    INT,
    latency_ms      INT,
    review_status   VARCHAR(20)     NOT NULL DEFAULT 'UNREVIEWED'
        CHECK (review_status IN ('UNREVIEWED', 'APPROVED', 'REJECTED', 'NEEDS_FIX')),
    review_note     TEXT,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_extract_raw_item ON travel_extract_log (raw_item_id);
CREATE INDEX idx_extract_type ON travel_extract_log (extract_type);
CREATE INDEX idx_extract_model ON travel_extract_log (extract_model);
CREATE INDEX idx_extract_prompt ON travel_extract_log (extract_prompt_version);
CREATE INDEX idx_extract_status ON travel_extract_log (extract_status);
CREATE INDEX idx_extract_review ON travel_extract_log (review_status);
CREATE INDEX idx_extract_input_hash ON travel_extract_log (input_hash);
CREATE INDEX idx_extract_created ON travel_extract_log (created_time);

CREATE TRIGGER trg_extract_log_updated
    BEFORE UPDATE ON travel_extract_log
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_extract_log IS 'LLM 提纯记录表，追踪提纯质量、成本，支持人工复盘';


-- -----------------------------------------------------------
-- 4. travel_content  清洗后的内容材料
-- -----------------------------------------------------------

CREATE TABLE travel_content (
    id              BIGSERIAL       PRIMARY KEY,
    raw_item_id     BIGINT          REFERENCES travel_raw_item(id),
    extract_log_id  BIGINT          REFERENCES travel_extract_log(id),
    city            VARCHAR(50)     NOT NULL,
    source_platform VARCHAR(20)     NOT NULL
        CHECK (source_platform IN ('xhs', 'amap', 'dianping', 'ctrip', 'mafengwo', 'manual')),
    source_type     VARCHAR(20)     NOT NULL
        CHECK (source_type IN ('note', 'guide', 'review', 'ranking', 'manual_route')),
    source_id       VARCHAR(200),
    source_url      TEXT,
    title           VARCHAR(500),
    author          VARCHAR(200),
    author_id       TEXT,
    author_url      TEXT,
    content_text    TEXT,
    content_summary TEXT,
    tags            JSONB           NOT NULL DEFAULT '[]'::JSONB,
    content_type_tags JSONB         NOT NULL DEFAULT '[]'::JSONB,
    quality_score   NUMERIC(3,1)    DEFAULT 0
        CHECK (quality_score >= 0 AND quality_score <= 10),
    hot_score       NUMERIC(10,2)   DEFAULT 0,
    language        VARCHAR(5)      NOT NULL DEFAULT 'zh',
    publish_time    TIMESTAMPTZ,
    captured_time   TIMESTAMPTZ,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_content_city ON travel_content (city);
CREATE INDEX idx_content_city_platform ON travel_content (city, source_platform);
CREATE INDEX idx_content_raw_item ON travel_content (raw_item_id);
CREATE INDEX idx_content_extract_log ON travel_content (extract_log_id);
CREATE INDEX idx_content_publish_time ON travel_content (publish_time);
CREATE INDEX idx_content_tags ON travel_content USING GIN (tags);
CREATE INDEX idx_content_type_tags ON travel_content USING GIN (content_type_tags);

CREATE TRIGGER trg_content_updated
    BEFORE UPDATE ON travel_content
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_content IS '清洗后的内容材料，小红书笔记、马蜂窝攻略、携程介绍等';


-- -----------------------------------------------------------
-- 5. travel_place  标准化地点 / POI 核心表
-- -----------------------------------------------------------

CREATE TABLE travel_place (
    id              BIGSERIAL       PRIMARY KEY,
    city            VARCHAR(50)     NOT NULL,
    name            VARCHAR(200)    NOT NULL,
    normalized_name VARCHAR(200)    NOT NULL,
    place_type      VARCHAR(30)     NOT NULL
        CHECK (place_type IN (
            'attraction', 'restaurant', 'business_area', 'market',
            'park', 'museum', 'photo_spot', 'hotel', 'other'
        )),
    address         VARCHAR(500),
    longitude       NUMERIC(10,7)
        CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180)),
    latitude        NUMERIC(10,7)
        CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90)),
    tags            JSONB           NOT NULL DEFAULT '[]'::JSONB,
    alias_names     JSONB           NOT NULL DEFAULT '[]'::JSONB,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_place_city ON travel_place (city);
CREATE INDEX idx_place_city_type ON travel_place (city, place_type);
CREATE UNIQUE INDEX uk_place_city_normalized ON travel_place (city, normalized_name);
CREATE INDEX idx_place_name_trgm ON travel_place USING GIN (name gin_trgm_ops);
CREATE INDEX idx_place_normalized_trgm ON travel_place USING GIN (normalized_name gin_trgm_ops);
CREATE INDEX idx_place_tags ON travel_place USING GIN (tags);
CREATE INDEX idx_place_alias ON travel_place USING GIN (alias_names);

CREATE TRIGGER trg_place_updated
    BEFORE UPDATE ON travel_place
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_place IS '标准化地点表，景点、餐厅、商圈、夜市、公园、拍照点等';


-- -----------------------------------------------------------
-- 6. travel_place_source_map  地点跨平台映射
-- -----------------------------------------------------------

CREATE TABLE travel_place_source_map (
    id              BIGSERIAL       PRIMARY KEY,
    place_id        BIGINT          NOT NULL REFERENCES travel_place(id),
    source_platform VARCHAR(20)     NOT NULL
        CHECK (source_platform IN ('xhs', 'amap', 'dianping', 'ctrip', 'mafengwo', 'manual')),
    source_id       VARCHAR(200)    NOT NULL,
    source_name     VARCHAR(200),
    source_url      TEXT,
    confidence      NUMERIC(3,2)    NOT NULL DEFAULT 1.00
        CHECK (confidence >= 0 AND confidence <= 1),
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uk_place_source ON travel_place_source_map (source_platform, source_id);
CREATE INDEX idx_place_source_place ON travel_place_source_map (place_id);

CREATE TRIGGER trg_place_source_map_updated
    BEFORE UPDATE ON travel_place_source_map
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_place_source_map IS '同一地点在不同平台的映射关系';


-- -----------------------------------------------------------
-- 7. travel_content_place_mention  内容与地点关系
-- -----------------------------------------------------------

CREATE TABLE travel_content_place_mention (
    id              BIGSERIAL       PRIMARY KEY,
    content_id      BIGINT          NOT NULL REFERENCES travel_content(id),
    place_id        BIGINT          NOT NULL REFERENCES travel_place(id),
    place_name_text VARCHAR(200),
    mention_context TEXT,
    mention_type    VARCHAR(20)     NOT NULL DEFAULT 'recommend'
        CHECK (mention_type IN ('recommend', 'avoid', 'pass_by', 'food', 'photo', 'stay', 'transport')),
    sentiment       VARCHAR(10)     NOT NULL DEFAULT 'positive'
        CHECK (sentiment IN ('positive', 'neutral', 'negative')),
    tags            JSONB           NOT NULL DEFAULT '[]'::JSONB,
    route_order     SMALLINT,
    confidence      NUMERIC(3,2)    NOT NULL DEFAULT 1.00
        CHECK (confidence >= 0 AND confidence <= 1),
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_mention_content ON travel_content_place_mention (content_id);
CREATE INDEX idx_mention_place ON travel_content_place_mention (place_id);
CREATE INDEX idx_mention_type ON travel_content_place_mention (mention_type);
CREATE INDEX idx_mention_sentiment ON travel_content_place_mention (sentiment);
CREATE INDEX idx_mention_tags ON travel_content_place_mention USING GIN (tags);

COMMENT ON TABLE travel_content_place_mention IS '内容与地点的关系表，记录推荐、吐槽、路过、拍照等提及';


-- -----------------------------------------------------------
-- 8. travel_place_fact  地点事实（多来源）
-- -----------------------------------------------------------

CREATE TABLE travel_place_fact (
    id              BIGSERIAL       PRIMARY KEY,
    place_id        BIGINT          NOT NULL REFERENCES travel_place(id),
    source_platform VARCHAR(20)     NOT NULL
        CHECK (source_platform IN ('xhs', 'amap', 'dianping', 'ctrip', 'mafengwo', 'manual')),
    source_id       VARCHAR(200),
    source_url      TEXT,
    fact_type       VARCHAR(30)     NOT NULL
        CHECK (fact_type IN (
            'rating', 'avg_price', 'opening_hours', 'recommendation', 'warning',
            'visit_duration', 'best_time', 'crowd_level', 'traffic_tip', 'photo_tip',
            'food_recommendation'
        )),
    fact_value      JSONB           NOT NULL,
    confidence      NUMERIC(3,2)    NOT NULL DEFAULT 1.00
        CHECK (confidence >= 0 AND confidence <= 1),
    captured_time   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_fact_place ON travel_place_fact (place_id);
CREATE INDEX idx_fact_place_type ON travel_place_fact (place_id, fact_type);
CREATE INDEX idx_fact_platform ON travel_place_fact (source_platform);
CREATE INDEX idx_fact_captured ON travel_place_fact (captured_time);

COMMENT ON TABLE travel_place_fact IS '地点事实表，保存评分、人均、营业时间、推荐理由等来源事实';


-- -----------------------------------------------------------
-- 9. travel_canonical_place  v0.6.15 trusted city POI master
-- -----------------------------------------------------------

CREATE TABLE travel_canonical_place (
    place_id            BIGSERIAL PRIMARY KEY,
    canonical_name      TEXT NOT NULL,
    city                TEXT NOT NULL,
    district            TEXT,
    address             TEXT,
    adcode              TEXT,
    latitude            DOUBLE PRECISION
        CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90)),
    longitude           DOUBLE PRECISION
        CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180)),
    amap_poi_id         TEXT,
    place_type          TEXT NOT NULL,
    category_tags       JSONB NOT NULL DEFAULT '[]'::JSONB,
    typical_visit_minutes SMALLINT
        CHECK (
            typical_visit_minutes IS NULL
            OR typical_visit_minutes BETWEEN 15 AND 480
        ),
    typical_visit_source TEXT
        CHECK (
            typical_visit_source IS NULL
            OR typical_visit_source IN ('manual', 'amap', 'xhs_median')
        ),
    typical_visit_confidence NUMERIC(3,2)
        CHECK (
            typical_visit_confidence IS NULL
            OR (typical_visit_confidence >= 0 AND typical_visit_confidence <= 1)
        ),
    typical_visit_updated_at TIMESTAMPTZ,
    source_type         TEXT NOT NULL
        CHECK (source_type IN ('manual', 'xhs', 'xhs_migrated', 'amap', 'public', 'official')),
    trust_level         TEXT NOT NULL DEFAULT 'candidate'
        CHECK (trust_level IN ('trusted', 'candidate', 'rejected')),
    review_status       TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (review_status IN ('auto_accepted', 'reviewed', 'pending_review', 'rejected')),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    contextual_only     BOOLEAN NOT NULL DEFAULT FALSE,
    amap_rating         NUMERIC(3,1),
    amap_avg_price      NUMERIC(8,2),
    amap_open_time      TEXT,
    amap_data_captured_at TIMESTAMPTZ,
    geo_status          TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (geo_status IN ('resolved', 'coordinate_only', 'unresolved', 'rejected')),
    base_priority       INTEGER NOT NULL DEFAULT 50
        CHECK (base_priority >= 1 AND base_priority <= 100),
    migration_batch_id  TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uq_canonical_city_name
    ON travel_canonical_place (city, canonical_name);
CREATE UNIQUE INDEX uq_canonical_amap_poi_id
    ON travel_canonical_place (amap_poi_id)
    WHERE amap_poi_id IS NOT NULL;
CREATE INDEX idx_canonical_city_active_geo
    ON travel_canonical_place (city, is_active, geo_status)
    WHERE is_active = TRUE;
CREATE INDEX idx_canonical_trust_review
    ON travel_canonical_place (trust_level, review_status);
CREATE INDEX idx_canonical_city_type_priority
    ON travel_canonical_place (city, place_type, base_priority DESC);

CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_canonical_place_updated
    BEFORE UPDATE ON travel_canonical_place
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE TABLE travel_place_alias (
    alias_id            BIGSERIAL PRIMARY KEY,
    canonical_place_id  BIGINT NOT NULL REFERENCES travel_canonical_place(place_id) ON DELETE CASCADE,
    alias_name          TEXT NOT NULL,
    normalized_alias    TEXT NOT NULL,
    relation_type       TEXT NOT NULL
        CHECK (relation_type IN ('alias_same', 'nearby_distinct', 'part_of', 'rejected')),
    status              TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('confirmed', 'candidate', 'rejected')),
    source              TEXT NOT NULL DEFAULT 'rule'
        CHECK (source IN ('manual', 'migrated', 'rule', 'xhs')),
    confidence          REAL DEFAULT 0.5
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_alias_canonical_normalized
        UNIQUE (canonical_place_id, normalized_alias)
);

CREATE INDEX idx_alias_normalized_status
    ON travel_place_alias (normalized_alias, status);
CREATE INDEX idx_alias_canonical_id
    ON travel_place_alias (canonical_place_id);

CREATE TABLE travel_canonical_place_source (
    source_id           BIGSERIAL PRIMARY KEY,
    canonical_place_id  BIGINT NOT NULL REFERENCES travel_canonical_place(place_id) ON DELETE CASCADE,
    source_type         TEXT NOT NULL
        CHECK (source_type IN (
            'official', 'amap_list', 'dianping_list', 'ctrip_list',
            'mafengwo_list', 'xhs_migrated', 'xhs', 'manual'
        )),
    source_name         TEXT NOT NULL,
    source_url          TEXT,
    source_rank         INTEGER,
    source_place_name   TEXT,
    evidence_summary    TEXT,
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_canonical_source_place
    ON travel_canonical_place_source (canonical_place_id);
CREATE INDEX idx_canonical_source_type
    ON travel_canonical_place_source (source_type);
CREATE UNIQUE INDEX uq_canonical_source_identity
    ON travel_canonical_place_source (
        canonical_place_id,
        source_type,
        source_name,
        COALESCE(source_url, ''),
        COALESCE(source_place_name, ''),
        COALESCE(source_rank, -1)
    );

COMMENT ON TABLE travel_canonical_place IS 'v0.6.15 trusted canonical POI master for city itinerary generation';
COMMENT ON TABLE travel_place_alias IS 'v0.6.15 canonical POI alias authority; confirmed DB aliases are authoritative';
COMMENT ON TABLE travel_canonical_place_source IS 'v0.6.15 factual source evidence for canonical POI admission, without platform body/comment text';

CREATE TABLE travel_canonical_place_review_event (
    event_id              BIGSERIAL PRIMARY KEY,
    canonical_place_id    BIGINT NOT NULL REFERENCES travel_canonical_place(place_id),
    action                TEXT NOT NULL
        CHECK (action IN (
            'candidate_create', 'auto_approve', 'approve', 'reject',
            'merge', 'reopen', 'downgrade'
        )),
    from_trust_level       TEXT,
    from_review_status     TEXT,
    to_trust_level         TEXT,
    to_review_status       TEXT,
    reason_code            TEXT NOT NULL,
    reason_text            TEXT,
    operator_id            TEXT NOT NULL DEFAULT 'system',
    merge_target_place_id  BIGINT REFERENCES travel_canonical_place(place_id),
    rule_version           TEXT,
    metadata               JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_canonical_review_event_place
    ON travel_canonical_place_review_event (canonical_place_id, created_at DESC);
CREATE INDEX idx_canonical_review_event_action
    ON travel_canonical_place_review_event (action, created_at DESC);


-- -----------------------------------------------------------
-- 9c. travel_place_fact_draft  人工地点事实草稿与审核
-- -----------------------------------------------------------

CREATE TABLE travel_place_fact_draft (
    id                  BIGSERIAL PRIMARY KEY,
    place_id            BIGINT NOT NULL
        REFERENCES travel_canonical_place(place_id),
    fact_text           TEXT NOT NULL
        CHECK (NULLIF(BTRIM(fact_text), '') IS NOT NULL),
    source_url          TEXT NOT NULL
        CHECK (NULLIF(BTRIM(source_url), '') IS NOT NULL),
    source_quote        TEXT NOT NULL
        CHECK (NULLIF(BTRIM(source_quote), '') IS NOT NULL),
    status              VARCHAR(20) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'rejected', 'published')),
    review_note         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at         TIMESTAMPTZ,
    published_fact_id   BIGINT UNIQUE REFERENCES travel_place_fact(id),
    CONSTRAINT chk_place_fact_draft_review_state
        CHECK (
            (status = 'draft'
                AND reviewed_at IS NULL
                AND published_fact_id IS NULL)
            OR (status IN ('approved', 'rejected')
                AND reviewed_at IS NOT NULL
                AND published_fact_id IS NULL)
            OR (status = 'published'
                AND reviewed_at IS NOT NULL
                AND published_fact_id IS NOT NULL)
        ),
    CONSTRAINT chk_place_fact_draft_rejection_note
        CHECK (
            status <> 'rejected'
            OR NULLIF(BTRIM(review_note), '') IS NOT NULL
        )
);

CREATE INDEX idx_place_fact_draft_status_created
    ON travel_place_fact_draft (status, created_at);
CREATE INDEX idx_place_fact_draft_place_status
    ON travel_place_fact_draft (place_id, status);

COMMENT ON TABLE travel_place_fact_draft IS
    'Human-reviewed canonical POI fact drafts; only approved rows may be published to travel_place_fact';
COMMENT ON COLUMN travel_place_fact_draft.place_id IS
    'Canonical POI id; publication resolves the linked travel_place id through travel_place_summary';
COMMENT ON COLUMN travel_place_fact_draft.published_fact_id IS
    'Idempotency marker referencing the single travel_place_fact row created for this draft';


-- -----------------------------------------------------------
-- 10. travel_place_summary  地点聚合摘要（hermes 快查）
-- -----------------------------------------------------------

CREATE TABLE travel_place_summary (
    place_id            BIGINT      PRIMARY KEY REFERENCES travel_place(id),
    canonical_place_id  BIGINT      REFERENCES travel_canonical_place(place_id),
    city                VARCHAR(50) NOT NULL,
    name                VARCHAR(200) NOT NULL,
    place_type          VARCHAR(30) NOT NULL
        CHECK (place_type IN (
            'attraction', 'restaurant', 'business_area', 'market',
            'park', 'museum', 'photo_spot', 'hotel', 'other'
        )),
    address             VARCHAR(500),
    longitude           NUMERIC(10,7)
        CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180)),
    latitude            NUMERIC(10,7)
        CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90)),
    tags                JSONB       NOT NULL DEFAULT '[]'::JSONB,
    mention_count_30d   INT         NOT NULL DEFAULT 0,
    positive_count_30d  INT         NOT NULL DEFAULT 0,
    negative_count_30d  INT         NOT NULL DEFAULT 0,
    source_count        INT         NOT NULL DEFAULT 0,
    latest_captured_time TIMESTAMPTZ,
    rating              NUMERIC(3,1),
    avg_price           NUMERIC(8,2),
    opening_hours       VARCHAR(200),
    top_reasons         JSONB       NOT NULL DEFAULT '[]'::JSONB,
    cleaned_evidence    JSONB,
    warnings            JSONB       NOT NULL DEFAULT '[]'::JSONB,
    hot_score           NUMERIC(10,2) NOT NULL DEFAULT 0,
    quality_score       NUMERIC(3,1) NOT NULL DEFAULT 0
        CHECK (quality_score >= 0 AND quality_score <= 10),
    recommend_score     NUMERIC(3,1) NOT NULL DEFAULT 0
        CHECK (recommend_score >= 0 AND recommend_score <= 10),
    updated_time        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_summary_city ON travel_place_summary (city);
CREATE INDEX idx_summary_city_type ON travel_place_summary (city, place_type);
CREATE INDEX idx_summary_recommend ON travel_place_summary (city, recommend_score DESC);
CREATE INDEX idx_summary_hot ON travel_place_summary (city, hot_score DESC);
CREATE UNIQUE INDEX uq_summary_canonical
    ON travel_place_summary (canonical_place_id)
    WHERE canonical_place_id IS NOT NULL;
CREATE INDEX idx_summary_canonical_place ON travel_place_summary (canonical_place_id);
CREATE INDEX idx_summary_tags ON travel_place_summary USING GIN (tags);
CREATE INDEX idx_summary_reasons ON travel_place_summary USING GIN (top_reasons);
CREATE INDEX idx_summary_warnings ON travel_place_summary USING GIN (warnings);

CREATE TRIGGER trg_summary_updated
    BEFORE UPDATE ON travel_place_summary
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_place_summary IS '地点聚合摘要表，由定时任务刷新，yuntu-travel 快速查询入口';
COMMENT ON COLUMN travel_place_summary.canonical_place_id IS 'v0.6.15 attachment from summary evidence to trusted canonical POI';


-- -----------------------------------------------------------
-- 10. travel_plan_record  攻略生成记录
-- -----------------------------------------------------------

CREATE TABLE travel_plan_record (
    id              BIGSERIAL       PRIMARY KEY,
    user_query      TEXT            NOT NULL,
    from_city       VARCHAR(50),
    to_city         VARCHAR(50)     NOT NULL,
    start_date      DATE,
    end_date        DATE,
    days            SMALLINT        NOT NULL CHECK (days > 0),
    nights          SMALLINT        CHECK (nights >= 0),
    people_count    SMALLINT        DEFAULT 1 CHECK (people_count > 0),
    preferences     JSONB           NOT NULL DEFAULT '[]'::JSONB,
    avoid           JSONB           NOT NULL DEFAULT '[]'::JSONB,
    notes           TEXT,
    used_content_ids JSONB          NOT NULL DEFAULT '[]'::JSONB,
    used_place_ids  JSONB           NOT NULL DEFAULT '[]'::JSONB,
    used_fact_ids   JSONB           NOT NULL DEFAULT '[]'::JSONB,
    generated_plan  TEXT            NOT NULL,
    plan_json       JSONB,
    prompt_text     TEXT,
    model_name      VARCHAR(50),
    quality_feedback TEXT,
    quality_metrics JSONB,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_plan_to_city ON travel_plan_record (to_city);
CREATE INDEX idx_plan_created ON travel_plan_record (created_time);
CREATE INDEX idx_plan_preferences ON travel_plan_record USING GIN (preferences);

COMMENT ON TABLE travel_plan_record IS '攻略生成记录表，追踪用户输入、引用数据、生成结果，用于复盘和优化';


-- -----------------------------------------------------------
-- 11. travel_trip_job  v0.2 旅行规划异步任务
-- -----------------------------------------------------------

CREATE TABLE travel_trip_job (
    id              BIGSERIAL       PRIMARY KEY,
    job_id          VARCHAR(32)     NOT NULL UNIQUE,
    request_id      VARCHAR(512)    NOT NULL UNIQUE,
    source          VARCHAR(30)     NOT NULL,
    conversation_id VARCHAR(200)    NOT NULL,
    user_display_name VARCHAR(100),
    user_query      TEXT            NOT NULL,
    normalized_query TEXT,
    normalized_query_hash VARCHAR(64),
    status          VARCHAR(20)     NOT NULL
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'TIMEOUT', 'REJECTED')),
    current_stage   VARCHAR(30)     NOT NULL
        CHECK (current_stage IN (
            'PENDING', 'INTENT_PARSER', 'DATA_RETRIEVAL', 'SEMANTIC_GROUPING',
            'POI_SELECTION',
            'ROUTE_PLANNING',
            'FINAL_WRITER',
            'HERMES_REVIEW', 'REVIEW_TAXONOMY', 'WRITER_REPAIR',
            'REVIEW_TAXONOMY_AFTER_REPAIR',
            'PUBLISH_RETRY', 'PERSISTING', 'SUCCESS', 'FAILED', 'TIMEOUT',
            'CITY_GATE_REJECTED'
        )),
    result_type     VARCHAR(30)
        CHECK (result_type IS NULL OR result_type IN (
            'PLAN_READY', 'NO_CANDIDATES', 'NO_USABLE_ROUTE'
        )),
    trip_request_json JSONB,
    result_record_id BIGINT         REFERENCES travel_plan_record(id),
    reply_text      TEXT,
    plan_count      INT,
    queue_position  INT,
    error_code      VARCHAR(30)
        CHECK (error_code IS NULL OR error_code IN (
            'WORKFLOW_ERROR', 'LLM_ERROR', 'DB_ERROR', 'TIMEOUT', 'UNKNOWN',
            'PUBLISH_GATE_FAILED', 'SAFE_RENDER_FAILED',
            'CITY_PREPARING', 'CITY_COLLECTION_FAILED', 'CITY_DATA_INSUFFICIENT', 'CITY_DISABLED',
            'CITY_CLARIFICATION_REQUIRED',
            'WRITER_CAPACITY_BUSY', 'WRITER_ENDPOINTS_UNAVAILABLE'
        )),
    error_message   TEXT,
    error_detail    TEXT,
    projection_version BIGINT      NOT NULL DEFAULT 1
        CHECK (projection_version >= 1),
    trace_completeness VARCHAR(20)  NOT NULL DEFAULT 'UNKNOWN'
        CHECK (trace_completeness IN ('COMPLETE', 'PARTIAL', 'UNKNOWN')),
    guide_result_state VARCHAR(24)  NOT NULL DEFAULT 'NOT_APPLICABLE'
        CHECK (guide_result_state IN (
            'NOT_APPLICABLE', 'LEGAL_NO_GUIDE', 'AVAILABLE', 'INCONSISTENT'
        )),
    request_field_provenance JSONB  NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(request_field_provenance) = 'object'),
    request_user_supplied_json JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(request_user_supplied_json) = 'object'),
    generation_policy_version VARCHAR(16) NOT NULL DEFAULT 'legacy'
        CONSTRAINT travel_trip_job_generation_policy_check
        CHECK (generation_policy_version IN ('legacy', 'v0.12')),
    delivery_json JSONB
        CONSTRAINT travel_trip_job_delivery_json_check CHECK ((
            (generation_policy_version = 'legacy' AND delivery_json IS NULL)
            OR (
                generation_policy_version = 'v0.12'
                AND jsonb_typeof(delivery_json) = 'object'
                AND delivery_json ?& ARRAY[
                    'schema_version', 'outcome', 'review_state',
                    'reason_code', 'selector_source'
                ]
                AND delivery_json ->> 'schema_version' = '1.0'
                AND delivery_json ->> 'outcome' IN (
                    'PENDING', 'DETAILED', 'BASIC', 'NO_ROUTE', 'FAILED'
                )
                AND delivery_json ->> 'review_state' IN (
                    'NOT_RUN', 'CODE_ONLY', 'MODEL_PASSED', 'UNAVAILABLE',
                    'ISSUES_UNRESOLVED'
                )
                AND delivery_json ->> 'selector_source' IN (
                    'LLM', 'DETERMINISTIC_FALLBACK', 'NOT_RUN'
                )
                AND CASE delivery_json ->> 'outcome'
                    WHEN 'PENDING' THEN
                        delivery_json ->> 'review_state' = 'NOT_RUN'
                        AND delivery_json -> 'reason_code' = 'null'::jsonb
                    WHEN 'DETAILED' THEN
                        delivery_json ->> 'review_state' IN ('CODE_ONLY', 'MODEL_PASSED')
                        AND delivery_json -> 'reason_code' = 'null'::jsonb
                    WHEN 'BASIC' THEN
                        (delivery_json ->> 'reason_code' = 'REVIEW_UNAVAILABLE'
                            AND delivery_json ->> 'review_state' = 'UNAVAILABLE')
                        OR (delivery_json ->> 'reason_code' = 'REVIEW_ISSUES_UNRESOLVED'
                            AND delivery_json ->> 'review_state' = 'ISSUES_UNRESOLVED')
                        OR (delivery_json ->> 'reason_code' = 'WRITER_UNAVAILABLE'
                            AND delivery_json ->> 'review_state' = 'NOT_RUN')
                    WHEN 'NO_ROUTE' THEN
                        delivery_json ->> 'review_state' = 'NOT_RUN'
                        AND delivery_json ->> 'reason_code' IN (
                            'NO_CANDIDATES', 'SEARCH_BUDGET_EXHAUSTED',
                            'CONSTRAINT_CONFLICT', 'ROUTE_DATA_UNAVAILABLE',
                            'NO_USABLE_ROUTE'
                        )
                    WHEN 'FAILED' THEN delivery_json ->> 'reason_code' IS NOT NULL
                    ELSE FALSE
                END
            )
        ) IS TRUE),
    route_diagnostics JSONB
        CONSTRAINT travel_trip_job_route_diagnostics_check CHECK ((
            route_diagnostics IS NULL
            OR (
                generation_policy_version = 'v0.12'
                AND jsonb_typeof(route_diagnostics) = 'object'
                AND route_diagnostics ?& ARRAY[
                    'schema_version', 'evidence_state', 'stop_reason',
                    'candidate_count', 'eliminated_counts', 'search',
                    'violations', 'truncated', 'replay_available'
                ]
                AND route_diagnostics ->> 'schema_version' = '1.0'
                AND octet_length(route_diagnostics::text) <= 16384
            )
        ) IS TRUE),
    route_replay_data JSONB,
    route_replay_expires_at TIMESTAMPTZ,
    CONSTRAINT travel_trip_job_route_replay_check CHECK ((
        (route_replay_data IS NULL AND route_replay_expires_at IS NULL)
        OR (
            generation_policy_version = 'v0.12'
            AND jsonb_typeof(route_replay_data) = 'object'
            AND octet_length(route_replay_data::text) <= 262144
            AND route_replay_expires_at IS NOT NULL
            AND route_replay_expires_at <= created_time + INTERVAL '7 days'
        )
    ) IS TRUE),
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    started_time    TIMESTAMPTZ,
    finished_time   TIMESTAMPTZ,
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_trip_job_status_created ON travel_trip_job (status, created_time);
CREATE INDEX idx_trip_job_source_conversation_status ON travel_trip_job (source, conversation_id, status);
CREATE INDEX idx_trip_job_user_display_name ON travel_trip_job (user_display_name);
CREATE INDEX idx_trip_job_created ON travel_trip_job (created_time);
CREATE INDEX idx_trip_job_normalized_hash ON travel_trip_job (normalized_query_hash);
CREATE INDEX idx_trip_job_result_record ON travel_trip_job (result_record_id);
CREATE INDEX idx_trip_job_route_replay_expiry ON travel_trip_job (route_replay_expires_at)
    WHERE route_replay_data IS NOT NULL;

CREATE TRIGGER trg_trip_job_updated
    BEFORE UPDATE ON travel_trip_job
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_trip_job IS 'v0.2 旅行规划异步任务状态表，供外层 Hermes 轮询进度和结果';


-- -----------------------------------------------------------
-- 12. travel_trip_job_step  async trip workflow observability
-- -----------------------------------------------------------

CREATE TABLE travel_trip_job_step (
    id                  BIGSERIAL       PRIMARY KEY,
    job_id              VARCHAR(32)     NOT NULL REFERENCES travel_trip_job(job_id) ON DELETE CASCADE,
    stage               VARCHAR(40)     NOT NULL
        CHECK (stage IN (
            'INTENT_PARSER', 'DATA_RETRIEVAL', 'SEMANTIC_GROUPING',
            'POI_SELECTION',
            'ROUTE_PLANNING',
            'FINAL_WRITER', 'HERMES_REVIEW', 'REVIEW_TAXONOMY',
            'WRITER_REPAIR', 'REVIEW_TAXONOMY_AFTER_REPAIR',
            'CONTENT_VALIDATION', 'SAFE_PLAN_RENDERER', 'PUBLISH_GATE',
            'PUBLISH_RETRY', 'PERSISTING', 'LLM_OBSERVABILITY'
        )),
    attempt             INT             NOT NULL DEFAULT 1,
    publish_retry_round INT             NOT NULL DEFAULT 0,
    status              VARCHAR(20)     NOT NULL
        CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED', 'TIMEOUT')),
    started_time        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    finished_time       TIMESTAMPTZ,
    latency_ms          INT,
    metadata            JSONB           NOT NULL DEFAULT '{}'::JSONB,
    projection_version  BIGINT          NOT NULL DEFAULT 1
        CHECK (projection_version >= 1),
    created_time        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time        TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_trip_job_step_job_stage ON travel_trip_job_step (job_id, stage, attempt, publish_retry_round);
CREATE INDEX idx_trip_job_step_stage_created ON travel_trip_job_step (stage, created_time);

CREATE TRIGGER trg_trip_job_step_updated
    BEFORE UPDATE ON travel_trip_job_step
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_trip_job_step IS 'Async trip workflow step timing and metadata for backend observability';

-- -----------------------------------------------------------
-- 12.0.1 Admin Control Plane v0.2 ordered projection Outbox
-- -----------------------------------------------------------

CREATE TABLE projection_stream_head (
    stream_id       SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (stream_id = 1),
    head_sequence   BIGINT      NOT NULL DEFAULT 0 CHECK (head_sequence >= 0),
    updated_time    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO projection_stream_head (stream_id, head_sequence)
VALUES (1, 0);

CREATE TABLE trip_projection_outbox (
    outbox_sequence    BIGINT       PRIMARY KEY CHECK (outbox_sequence >= 1),
    event_id           UUID         NOT NULL UNIQUE,
    event_type         VARCHAR(64)  NOT NULL
        CHECK (event_type = 'TRIP_PROJECTION_COMMITTED'),
    schema_version     VARCHAR(16)  NOT NULL
        CONSTRAINT trip_projection_outbox_schema_version_check
        CHECK (schema_version IN ('1.0', '1.1', '1.2')),
    aggregate_type     VARCHAR(32)  NOT NULL CHECK (aggregate_type = 'TRIP_JOB'),
    aggregate_id       VARCHAR(160) NOT NULL,
    aggregate_version  BIGINT       NOT NULL CHECK (aggregate_version >= 1),
    occurred_at        TIMESTAMPTZ  NOT NULL,
    payload            JSONB        NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    published_at       TIMESTAMPTZ,
    publish_attempts   INTEGER      NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
    last_publish_error VARCHAR(500)
);

CREATE INDEX idx_trip_projection_outbox_unpublished
    ON trip_projection_outbox (outbox_sequence)
    WHERE published_at IS NULL;
CREATE INDEX idx_trip_projection_outbox_aggregate
    ON trip_projection_outbox (aggregate_id, aggregate_version);

-- -----------------------------------------------------------
-- 12.1 travel_trip_failed_draft  P4.4-H1 terminal safe projection
-- -----------------------------------------------------------

CREATE TABLE travel_trip_failed_draft (
    id           BIGSERIAL   PRIMARY KEY,
    job_id       VARCHAR(32) NOT NULL
        REFERENCES travel_trip_job(job_id) ON DELETE RESTRICT,
    plans_json   JSONB       NOT NULL
        CHECK (jsonb_typeof(plans_json) = 'array'),
    created_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_trip_failed_draft_job UNIQUE (job_id)
);

CREATE INDEX idx_trip_failed_draft_created
    ON travel_trip_failed_draft (created_time DESC, id DESC);

COMMENT ON TABLE travel_trip_failed_draft IS
    'P4.4-H1 immutable safe Writer projection for terminal FAILED/TIMEOUT trip jobs';
COMMENT ON COLUMN travel_trip_failed_draft.plans_json IS
    'Array limited to plan_name, summary, plan_text, used_place_names, day_place_names';

-- -----------------------------------------------------------
-- 13. travel_amap_district and travel_amap_route_cache
-- -----------------------------------------------------------

CREATE TABLE travel_amap_district (
    adcode      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    level       TEXT NOT NULL,
    parent_code TEXT,
    center_lng  DOUBLE PRECISION,
    center_lat  DOUBLE PRECISION,
    citycode    TEXT,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_travel_amap_district_parent
    ON travel_amap_district(parent_code);
CREATE INDEX idx_travel_amap_district_level
    ON travel_amap_district(level);

CREATE TABLE travel_amap_route_cache (
    id                      BIGSERIAL       PRIMARY KEY,
    provider                VARCHAR(20)     NOT NULL DEFAULT 'amap',
    strategy                INT             NOT NULL DEFAULT 0,
    mode                    VARCHAR(10)     NOT NULL DEFAULT 'driving',
    origin_place_id         BIGINT          NOT NULL,
    destination_place_id    BIGINT          NOT NULL,
    origin_longitude        DOUBLE PRECISION,
    origin_latitude         DOUBLE PRECISION,
    destination_longitude   DOUBLE PRECISION,
    destination_latitude    DOUBLE PRECISION,
    distance_meters         INT             NOT NULL,
    duration_minutes        INT             NOT NULL,
    transit_steps_json      JSONB,
    hit_count               INT             NOT NULL DEFAULT 0,
    last_hit_time           TIMESTAMPTZ,
    created_time            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_travel_amap_route_cache_mode
        CHECK (mode IN ('driving', 'transit', 'walking', 'cycling')),
    CONSTRAINT uq_travel_amap_route_cache_provider_strategy_pair_mode
        UNIQUE (
            provider, strategy, origin_place_id, destination_place_id, mode
        )
);

CREATE INDEX idx_amap_route_cache_origin_dest
    ON travel_amap_route_cache (origin_place_id, destination_place_id);

CREATE TRIGGER trg_amap_route_cache_updated
    BEFORE UPDATE ON travel_amap_route_cache
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_amap_route_cache IS 'Cached Amap route durations for final selected commute legs';

-- -----------------------------------------------------------
-- 14. travel_plan_feedback  v0.4 用户显式反馈
-- -----------------------------------------------------------

CREATE TABLE travel_plan_feedback (
    id               BIGSERIAL      PRIMARY KEY,
    request_id       VARCHAR(512)   NOT NULL UNIQUE,
    result_record_id BIGINT         NOT NULL REFERENCES travel_plan_record(id),
    source           VARCHAR(30)    NOT NULL,
    conversation_id  VARCHAR(200)   NOT NULL,
    rating           VARCHAR(20)    NOT NULL
        CHECK (rating IN ('positive', 'negative')),
    feedback_text    TEXT           NOT NULL,
    created_time     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_plan_feedback_record ON travel_plan_feedback (result_record_id);
CREATE INDEX idx_plan_feedback_source_conversation_created
    ON travel_plan_feedback (source, conversation_id, created_time);

COMMENT ON TABLE travel_plan_feedback IS 'v0.4 用户对当前成功攻略的显式正负反馈，仅用于人工复盘';


-- -----------------------------------------------------------
-- 14.1 travel_export_artifact / travel_export_quota  v0.8.10.1 后端导出制品
-- -----------------------------------------------------------

CREATE TABLE travel_export_artifact (
    id               BIGSERIAL      PRIMARY KEY,
    artifact_id      VARCHAR(32)    NOT NULL UNIQUE,
    result_record_id BIGINT         NOT NULL REFERENCES travel_plan_record(id),
    artifact_type    VARCHAR(20)    NOT NULL
        CHECK (artifact_type IN ('pdf', 'share_image')),
    status           VARCHAR(20)    NOT NULL
        CHECK (status IN ('pending', 'running', 'ready', 'failed')),
    source_hash      VARCHAR(64)    NOT NULL,
    export_version   VARCHAR(64)    NOT NULL,
    storage_backend  VARCHAR(20)    NOT NULL DEFAULT 'local',
    storage_key      TEXT,
    filename         TEXT,
    mime_type        VARCHAR(100),
    byte_size        BIGINT,
    sha256           VARCHAR(64),
    text_length      INT,
    width_px         INT,
    height_px        INT,
    page_count       INT,
    metadata         JSONB          NOT NULL DEFAULT '{}'::JSONB,
    attempt_count    INT            NOT NULL DEFAULT 0,
    error_code       VARCHAR(50),
    error_message    TEXT,
    client_ip_hash   VARCHAR(64),
    created_time     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    started_time     TIMESTAMPTZ,
    finished_time    TIMESTAMPTZ,
    expires_time     TIMESTAMPTZ,
    updated_time     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_export_artifact_identity
        UNIQUE (result_record_id, artifact_type, source_hash, export_version)
);

CREATE INDEX idx_export_artifact_result_type
    ON travel_export_artifact (result_record_id, artifact_type);
CREATE INDEX idx_export_artifact_status_created
    ON travel_export_artifact (status, created_time);
CREATE INDEX idx_export_artifact_expires
    ON travel_export_artifact (expires_time);

CREATE TRIGGER trg_export_artifact_updated
    BEFORE UPDATE ON travel_export_artifact
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

CREATE TABLE travel_export_quota (
    id             BIGSERIAL      PRIMARY KEY,
    quota_day      DATE           NOT NULL,
    client_ip_hash VARCHAR(64)    NOT NULL,
    artifact_type  VARCHAR(20)    NOT NULL
        CHECK (artifact_type IN ('pdf', 'share_image')),
    generate_count INT            NOT NULL DEFAULT 0,
    created_time   TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_time   TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_export_quota_identity
        UNIQUE (quota_day, client_ip_hash, artifact_type)
);

CREATE TRIGGER trg_export_quota_updated
    BEFORE UPDATE ON travel_export_quota
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_export_artifact IS 'v0.8.10.1 backend export artifact metadata; binary files are stored outside DB';
COMMENT ON TABLE travel_export_quota IS 'v0.8.10.1 strong DB-backed export quota by Asia/Shanghai business day and client IP hash';


-- -----------------------------------------------------------
-- 13. travel_crawl_run  v0.2 内部采集运行父任务
-- -----------------------------------------------------------

CREATE TABLE travel_crawl_run (
    id              BIGSERIAL       PRIMARY KEY,
    platform        VARCHAR(32)     NOT NULL DEFAULT 'xhs'
        CHECK (platform IN ('xhs')),
    city            VARCHAR(64),
    keyword         VARCHAR(255),
    limit_count     INT             NOT NULL DEFAULT 10
        CHECK (limit_count >= 1 AND limit_count <= 50),
    trigger_source  VARCHAR(32)     NOT NULL DEFAULT 'manual'
        CHECK (trigger_source IN ('manual', 'hermes', 'cron')),
    status          VARCHAR(32)     NOT NULL DEFAULT 'PENDING'
        CHECK (status IN (
            'PENDING', 'RUNNING', 'SUCCESS', 'FAILED',
            'COOKIE_EXPIRED', 'PARTIAL_SUCCESS', 'TIMEOUT'
        )),
    run_extract     BOOLEAN         NOT NULL DEFAULT FALSE,
    refresh_summary BOOLEAN         NOT NULL DEFAULT FALSE,
    raw_count       INT             NOT NULL DEFAULT 0,
    insert_count    INT             NOT NULL DEFAULT 0,
    duplicate_count INT             NOT NULL DEFAULT 0,
    failed_count    INT             NOT NULL DEFAULT 0,
    error_code      VARCHAR(64),
    error_message   TEXT,
    stdout_summary  TEXT,
    stderr_summary  TEXT,
    started_time    TIMESTAMPTZ,
    finished_time   TIMESTAMPTZ,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CHECK (run_extract OR NOT refresh_summary)
);

CREATE INDEX idx_crawl_run_platform_city_created ON travel_crawl_run (platform, city, created_time);
CREATE INDEX idx_crawl_run_status_created ON travel_crawl_run (status, created_time);
CREATE INDEX idx_crawl_run_trigger_created ON travel_crawl_run (trigger_source, created_time);

ALTER TABLE travel_raw_item
    ADD CONSTRAINT fk_raw_item_crawl_run
    FOREIGN KEY (crawl_run_id) REFERENCES travel_crawl_run(id);

CREATE TRIGGER trg_crawl_run_updated
    BEFORE UPDATE ON travel_crawl_run
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_crawl_run IS 'v0.2 内部采集运行父任务，一次 Hermes 管理员触发对应一条记录';


-- -----------------------------------------------------------
-- 14. travel_crawl_run_step  v0.2 内部采集运行固定子步骤
-- -----------------------------------------------------------

CREATE TABLE travel_crawl_run_step (
    id              BIGSERIAL       PRIMARY KEY,
    run_id          BIGINT          NOT NULL REFERENCES travel_crawl_run(id) ON DELETE CASCADE,
    step_name       VARCHAR(32)     NOT NULL
        CHECK (step_name IN ('CRAWL', 'EXTRACT', 'POI_RESOLVE', 'REFRESH_SUMMARY')),
    status          VARCHAR(32)     NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED', 'TIMEOUT')),
    started_time    TIMESTAMPTZ,
    finished_time   TIMESTAMPTZ,
    raw_count       INT             NOT NULL DEFAULT 0,
    insert_count    INT             NOT NULL DEFAULT 0,
    duplicate_count INT             NOT NULL DEFAULT 0,
    failed_count    INT             NOT NULL DEFAULT 0,
    error_code      VARCHAR(64),
    error_message   TEXT,
    stdout_summary  TEXT,
    stderr_summary  TEXT,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uk_crawl_run_step_identity ON travel_crawl_run_step (run_id, step_name);
CREATE INDEX idx_crawl_run_step_run_status ON travel_crawl_run_step (run_id, status);
CREATE INDEX idx_crawl_run_step_name_status_created ON travel_crawl_run_step (step_name, status, created_time);

CREATE TRIGGER trg_crawl_run_step_updated
    BEFORE UPDATE ON travel_crawl_run_step
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_crawl_run_step IS 'v0.2+ internal crawl run fixed step status table: CRAWL / EXTRACT / POI_RESOLVE / REFRESH_SUMMARY';


-- -----------------------------------------------------------
-- 15. travel_city domain  v0.5 canonical city and quality
-- -----------------------------------------------------------

CREATE TABLE travel_city (
    id                      BIGSERIAL PRIMARY KEY,
    canonical_name          VARCHAR(64) NOT NULL UNIQUE,
    status                  VARCHAR(20) NOT NULL DEFAULT 'DISCOVERED'
        CHECK (status IN ('DISCOVERED', 'GRAY', 'ACTIVE', 'DISABLED')),
    active_confirmed_time   TIMESTAMPTZ,
    request_count_30d       INT NOT NULL DEFAULT 0 CHECK (request_count_30d >= 0),
    last_requested_time     TIMESTAMPTZ,
    last_quality_check_time TIMESTAMPTZ,
    last_refresh_time       TIMESTAMPTZ,
    next_refresh_time       TIMESTAMPTZ,
    disabled_reason         TEXT,
    created_time            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_time            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_city_status_next_refresh ON travel_city (status, next_refresh_time);

CREATE TABLE travel_city_alias (
    id           BIGSERIAL PRIMARY KEY,
    city_id      BIGINT NOT NULL REFERENCES travel_city(id) ON DELETE CASCADE,
    alias        VARCHAR(64) NOT NULL UNIQUE,
    created_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_time TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_city_alias_city ON travel_city_alias (city_id);

CREATE TABLE travel_city_demand (
    id                     BIGSERIAL PRIMARY KEY,
    city_id                BIGINT NOT NULL REFERENCES travel_city(id),
    request_id             VARCHAR(512) NOT NULL,
    source                 VARCHAR(30) NOT NULL,
    conversation_id        VARCHAR(200) NOT NULL,
    raw_query              TEXT NOT NULL,
    normalized_preferences JSONB NOT NULL DEFAULT '[]'::JSONB,
    created_time           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, request_id)
);

CREATE INDEX idx_city_demand_city_created ON travel_city_demand (city_id, created_time);

CREATE TABLE IF NOT EXISTS travel_place_demand (
    id BIGSERIAL PRIMARY KEY,
    city VARCHAR(64) NOT NULL,
    input_name VARCHAR(200) NOT NULL,
    normalized_name VARCHAR(200) NOT NULL,
    matched_place_id BIGINT REFERENCES travel_canonical_place(place_id),
    matched_city VARCHAR(64),
    match_status VARCHAR(20) NOT NULL CHECK (match_status IN ('matched','candidate','unmatched','cross_city')),
    source VARCHAR(30) NOT NULL CHECK (source IN ('user_must_include')),
    request_id VARCHAR(512) NOT NULL,
    conversation_id VARCHAR(200) NOT NULL DEFAULT '',
    created_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_place_demand_request UNIQUE (source, request_id, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_place_demand_city_status
    ON travel_place_demand (city, match_status, created_time);
CREATE INDEX IF NOT EXISTS idx_place_demand_matched_place
    ON travel_place_demand (matched_place_id)
    WHERE matched_place_id IS NOT NULL;

CREATE TABLE travel_city_quality_snapshot (
    id                       BIGSERIAL PRIMARY KEY,
    city_id                  BIGINT NOT NULL REFERENCES travel_city(id),
    valid_place_count        INT NOT NULL DEFAULT 0 CHECK (valid_place_count >= 0),
    valid_evidence_count     INT NOT NULL DEFAULT 0 CHECK (valid_evidence_count >= 0),
    covered_categories       JSONB NOT NULL DEFAULT '[]'::JSONB,
    successful_base_keywords JSONB NOT NULL DEFAULT '[]'::JSONB,
    blocking_issues          JSONB NOT NULL DEFAULT '[]'::JSONB,
    gray_eligible            BOOLEAN NOT NULL DEFAULT FALSE,
    active_eligible          BOOLEAN NOT NULL DEFAULT FALSE,
    canonical_route_eligible_activity_count INT NOT NULL DEFAULT 0 CHECK (canonical_route_eligible_activity_count >= 0),
    canonical_route_eligible_food_count INT NOT NULL DEFAULT 0 CHECK (canonical_route_eligible_food_count >= 0),
    canonical_route_eligible_type_coverage INT NOT NULL DEFAULT 0 CHECK (canonical_route_eligible_type_coverage >= 0),
    canonical_geo_resolved_ratio NUMERIC(5,4) NOT NULL DEFAULT 0 CHECK (canonical_geo_resolved_ratio >= 0 AND canonical_geo_resolved_ratio <= 1),
    canonical_quality_pass BOOLEAN NOT NULL DEFAULT FALSE,
    summary_effective_places INT NOT NULL DEFAULT 0 CHECK (summary_effective_places >= 0),
    summary_effective_evidence INT NOT NULL DEFAULT 0 CHECK (summary_effective_evidence >= 0),
    summary_type_coverage INT NOT NULL DEFAULT 0 CHECK (summary_type_coverage >= 0),
    evidence_quality_pass BOOLEAN NOT NULL DEFAULT FALSE,
    checked_time             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_city_quality_snapshot_city_checked
    ON travel_city_quality_snapshot (city_id, checked_time DESC);

CREATE TABLE travel_city_keyword_review (
    id             BIGSERIAL PRIMARY KEY,
    city_id        BIGINT NOT NULL REFERENCES travel_city(id) ON DELETE CASCADE,
    keyword        VARCHAR(64) NOT NULL,
    status         VARCHAR(20) NOT NULL DEFAULT 'PENDING_REVIEW'
        CHECK (status IN ('PENDING_REVIEW', 'APPROVED', 'REJECTED')),
    request_count  INT NOT NULL DEFAULT 1 CHECK (request_count >= 1),
    reviewed_time  TIMESTAMPTZ,
    created_time   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_time   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (city_id, keyword)
);

CREATE INDEX idx_city_keyword_review_status
    ON travel_city_keyword_review (status, created_time);

CREATE TABLE travel_city_extension_keyword (
    id           BIGSERIAL PRIMARY KEY,
    city_id      BIGINT NOT NULL REFERENCES travel_city(id) ON DELETE CASCADE,
    keyword      VARCHAR(64) NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (city_id, keyword)
);

CREATE INDEX idx_city_extension_keyword_city_enabled
    ON travel_city_extension_keyword (city_id, enabled);

CREATE OR REPLACE FUNCTION fn_validate_travel_city_identity()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_TABLE_NAME = 'travel_city' THEN
        PERFORM pg_advisory_xact_lock(
            hashtextextended(NEW.canonical_name, 2026050502)
        );
        IF EXISTS (
            SELECT 1 FROM travel_city_alias
            WHERE alias = NEW.canonical_name AND city_id <> NEW.id
        ) THEN
            RAISE EXCEPTION 'canonical city name conflicts with an existing alias';
        END IF;
    ELSE
        PERFORM pg_advisory_xact_lock(
            hashtextextended(NEW.alias, 2026050502)
        );
        IF EXISTS (
            SELECT 1 FROM travel_city
            WHERE canonical_name = NEW.alias AND id <> NEW.city_id
        ) THEN
            RAISE EXCEPTION 'city alias conflicts with an existing canonical city';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_city_identity_check
    BEFORE INSERT OR UPDATE OF canonical_name ON travel_city
    FOR EACH ROW EXECUTE FUNCTION fn_validate_travel_city_identity();
CREATE TRIGGER trg_city_alias_identity_check
    BEFORE INSERT OR UPDATE OF city_id, alias ON travel_city_alias
    FOR EACH ROW EXECUTE FUNCTION fn_validate_travel_city_identity();

CREATE OR REPLACE FUNCTION fn_limit_travel_city_extension_keywords()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.city_id::text, 2026050503)
    );
    IF NEW.enabled AND (
        SELECT COUNT(*)
        FROM travel_city_extension_keyword
        WHERE city_id = NEW.city_id
          AND enabled
          AND id <> COALESCE(NEW.id, 0)
    ) >= 10 THEN
        RAISE EXCEPTION 'a city can have at most 10 enabled extension keywords';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_city_extension_keyword_limit
    BEFORE INSERT OR UPDATE OF city_id, enabled ON travel_city_extension_keyword
    FOR EACH ROW EXECUTE FUNCTION fn_limit_travel_city_extension_keywords();

CREATE TRIGGER trg_city_updated
    BEFORE UPDATE ON travel_city
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();
CREATE TRIGGER trg_city_alias_updated
    BEFORE UPDATE ON travel_city_alias
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();
CREATE TRIGGER trg_city_keyword_review_updated
    BEFORE UPDATE ON travel_city_keyword_review
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();
CREATE TRIGGER trg_city_extension_keyword_updated
    BEFORE UPDATE ON travel_city_extension_keyword
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_city IS 'v0.5 canonical city identity and recommendation availability state';
COMMENT ON TABLE travel_city_alias IS 'v0.5 administrator-maintained alias to one canonical city';
COMMENT ON TABLE travel_city_demand IS 'v0.5 idempotent complete recommendation demand by source and request_id';
COMMENT ON TABLE travel_city_quality_snapshot IS 'v0.5 reproducible deterministic city quality inspection snapshot';
COMMENT ON TABLE travel_city_keyword_review IS 'v0.5 candidate extension keyword requiring administrator review';
COMMENT ON TABLE travel_city_extension_keyword IS 'v0.5 approved long-term city extension keyword, max 10 enabled per city';


-- -----------------------------------------------------------
-- 16. travel_city_crawl_batch  v0.5 city-level serial crawl batches
-- -----------------------------------------------------------

CREATE TABLE travel_city_crawl_batch (
    id              BIGSERIAL       PRIMARY KEY,
    city_id         BIGINT          NOT NULL REFERENCES travel_city(id),
    trigger_source  VARCHAR(32)     NOT NULL
        CHECK (trigger_source IN ('demand', 'manual', 'refresh', 'retry')),
    reason          VARCHAR(64)     NOT NULL,
    limit_per_keyword INT           NOT NULL DEFAULT 20
        CHECK (limit_per_keyword BETWEEN 1 AND 50),
    status          VARCHAR(32)     NOT NULL DEFAULT 'PENDING'
        CHECK (status IN (
            'PENDING', 'RUNNING', 'SUCCESS', 'PARTIAL_SUCCESS',
            'FAILED', 'COOKIE_EXPIRED', 'TIMEOUT'
        )),
    extract_status  VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
        CHECK (extract_status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED', 'TIMEOUT')),
    poi_resolve_status VARCHAR(20)  NOT NULL DEFAULT 'PENDING'
        CHECK (poi_resolve_status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED', 'TIMEOUT')),
    refresh_status  VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
        CHECK (refresh_status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED', 'TIMEOUT')),
    quality_status  VARCHAR(20)     NOT NULL DEFAULT 'PENDING'
        CHECK (quality_status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED')),
    heartbeat_time  TIMESTAMPTZ,
    started_time    TIMESTAMPTZ,
    finished_time   TIMESTAMPTZ,
    error_code      VARCHAR(64),
    error_message   TEXT,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uk_city_crawl_batch_global_active
    ON travel_city_crawl_batch ((TRUE))
    WHERE status IN ('PENDING', 'RUNNING');
CREATE INDEX idx_city_crawl_batch_city_status
    ON travel_city_crawl_batch (city_id, status, created_time DESC);

CREATE TABLE travel_city_crawl_batch_item (
    id              BIGSERIAL       PRIMARY KEY,
    batch_id        BIGINT          NOT NULL REFERENCES travel_city_crawl_batch(id) ON DELETE CASCADE,
    keyword         VARCHAR(64)     NOT NULL,
    keyword_type    VARCHAR(20)     NOT NULL DEFAULT 'BASE'
        CHECK (keyword_type IN ('BASE', 'EXTENSION')),
    status          VARCHAR(32)     NOT NULL DEFAULT 'PENDING'
        CHECK (status IN (
            'PENDING', 'RUNNING', 'SUCCESS', 'FAILED',
            'COOKIE_EXPIRED', 'TIMEOUT', 'SKIPPED'
        )),
    crawl_run_id    BIGINT          REFERENCES travel_crawl_run(id),
    error_code      VARCHAR(64),
    error_message   TEXT,
    started_time    TIMESTAMPTZ,
    finished_time   TIMESTAMPTZ,
    created_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_time    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id, keyword)
);

CREATE INDEX idx_city_crawl_batch_item_batch_status
    ON travel_city_crawl_batch_item (batch_id, status, id);
CREATE INDEX idx_city_crawl_batch_item_run
    ON travel_city_crawl_batch_item (crawl_run_id);

CREATE TRIGGER trg_city_crawl_batch_updated
    BEFORE UPDATE ON travel_city_crawl_batch
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();
CREATE TRIGGER trg_city_crawl_batch_item_updated
    BEFORE UPDATE ON travel_city_crawl_batch_item
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_time();

COMMENT ON TABLE travel_city_crawl_batch IS 'v0.5 city-level serial crawl orchestration batch';
COMMENT ON TABLE travel_city_crawl_batch_item IS 'v0.5 city batch keyword item linked to one lower-level crawl run';


-- -----------------------------------------------------------
-- 17. v0.9.4.2 Writer relay routing and durable capacity leases
-- -----------------------------------------------------------

CREATE TABLE travel_relay_endpoint_state (
    endpoint_name               VARCHAR(64) PRIMARY KEY,
    participation               VARCHAR(16) NOT NULL CHECK (participation IN ('ACTIVE', 'CANARY', 'DISABLED')),
    previous_participation      VARCHAR(16) NOT NULL CHECK (previous_participation IN ('ACTIVE', 'CANARY', 'DISABLED')),
    circuit_state               VARCHAR(16) NOT NULL DEFAULT 'CLOSED' CHECK (circuit_state IN ('CLOSED', 'OPEN', 'HALF_OPEN', 'QUARANTINED')),
    qualification_state         VARCHAR(16) NOT NULL DEFAULT 'QUALIFIED' CHECK (qualification_state IN ('QUALIFIED', 'UNQUALIFIED')),
    max_inflight                SMALLINT NOT NULL CHECK (max_inflight >= 0),
    routing_weight              SMALLINT NOT NULL CHECK (routing_weight >= 0),
    configuration_fingerprint   VARCHAR(64) NOT NULL DEFAULT '',
    selection_current           BIGINT NOT NULL DEFAULT 0,
    selection_count             BIGINT NOT NULL DEFAULT 0,
    peak_inflight               INT NOT NULL DEFAULT 0 CHECK (peak_inflight >= 0),
    observation_count           BIGINT NOT NULL DEFAULT 0,
    transport_success_count     BIGINT NOT NULL DEFAULT 0,
    timeout_count               BIGINT NOT NULL DEFAULT 0,
    auth_policy_rejection_count BIGINT NOT NULL DEFAULT 0,
    contract_valid_count        BIGINT NOT NULL DEFAULT 0,
    pending_policy_count        INT NOT NULL DEFAULT 0 CHECK (pending_policy_count >= 0),
    consecutive_invalid_output  INT NOT NULL DEFAULT 0,
    consecutive_transient_failures SMALLINT NOT NULL DEFAULT 0 CHECK (consecutive_transient_failures >= 0),
    cooldown_level              SMALLINT NOT NULL DEFAULT 0 CHECK (cooldown_level BETWEEN 0 AND 2),
    cooldown_until              TIMESTAMPTZ,
    probe_owner                 VARCHAR(128),
    probe_lease_expires_at      TIMESTAMPTZ,
    probe_kind                  VARCHAR(16) CHECK (probe_kind IS NULL OR probe_kind IN ('automatic', 'manual')),
    recovery_origin_circuit_state VARCHAR(16)
        CHECK (
            recovery_origin_circuit_state IS NULL
            OR recovery_origin_circuit_state IN ('OPEN', 'QUARANTINED')
        ),
    active_operation_id         UUID,
    state_generation            BIGINT NOT NULL DEFAULT 0 CHECK (state_generation >= 0),
    manual_retry_not_before     TIMESTAMPTZ,
    manual_retry_level          SMALLINT NOT NULL DEFAULT 0 CHECK (manual_retry_level BETWEEN 0 AND 2),
    operator_actor              VARCHAR(128),
    operator_reason             VARCHAR(500),
    operator_updated_at         TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_writer_relay_half_open_metadata CHECK (
        (
            circuit_state = 'HALF_OPEN'
            AND probe_kind IN ('automatic', 'manual')
            AND probe_owner IS NOT NULL
            AND probe_lease_expires_at IS NOT NULL
            AND recovery_origin_circuit_state IN ('OPEN', 'QUARANTINED')
            AND (
                probe_kind <> 'manual'
                OR active_operation_id IS NOT NULL
            )
        )
        OR (
            circuit_state <> 'HALF_OPEN'
            AND probe_kind IS NULL
            AND probe_owner IS NULL
            AND probe_lease_expires_at IS NULL
            AND recovery_origin_circuit_state IS NULL
            AND active_operation_id IS NULL
        )
    )
);

CREATE TABLE travel_writer_dispatch (
    dispatch_id          UUID PRIMARY KEY,
    job_id               VARCHAR(128),
    logical_call_id      UUID NOT NULL,
    ordinal              SMALLINT NOT NULL CHECK (ordinal IN (0, 1)),
    call_reason          VARCHAR(128) NOT NULL,
    endpoint_name        VARCHAR(64) NOT NULL REFERENCES travel_relay_endpoint_state(endpoint_name),
    model                VARCHAR(128) NOT NULL,
    release_identity     VARCHAR(32) NOT NULL,
    state                VARCHAR(20) NOT NULL CHECK (state IN ('CLAIMED', 'DISPATCHED', 'SUCCEEDED', 'FAILED', 'OUTCOME_UNKNOWN')),
    lease_expires_at     TIMESTAMPTZ NOT NULL,
    dispatched_at        TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ,
    latency_ms           INT CHECK (latency_ms IS NULL OR latency_ms >= 0),
    safe_failure_class   VARCHAR(64),
    http_status_class    VARCHAR(8),
    failure_signature    VARCHAR(64),
    half_open_trial      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (logical_call_id, ordinal)
);

CREATE INDEX idx_writer_dispatch_active_lease
    ON travel_writer_dispatch (endpoint_name, lease_expires_at)
    WHERE state IN ('CLAIMED', 'DISPATCHED');
CREATE INDEX idx_writer_dispatch_job_created
    ON travel_writer_dispatch (job_id, created_at DESC);
CREATE INDEX idx_writer_dispatch_failure_signature
    ON travel_writer_dispatch (logical_call_id, failure_signature, endpoint_name)
    WHERE failure_signature IS NOT NULL;

CREATE UNIQUE INDEX uq_writer_relay_single_half_open
    ON travel_relay_endpoint_state ((1))
    WHERE circuit_state = 'HALF_OPEN';

INSERT INTO travel_relay_endpoint_state (
    endpoint_name, participation, previous_participation,
    max_inflight, routing_weight, qualification_state
) VALUES
    ('aixoras', 'CANARY', 'CANARY', 2, 2, 'QUALIFIED'),
    ('centos', 'ACTIVE', 'ACTIVE', 3, 3, 'QUALIFIED'),
    ('shuai', 'ACTIVE', 'ACTIVE', 3, 3, 'QUALIFIED'),
    ('kuaipao', 'DISABLED', 'DISABLED', 0, 0, 'UNQUALIFIED'),
    ('gateai', 'CANARY', 'CANARY', 2, 2, 'QUALIFIED'),
    ('venlacy', 'DISABLED', 'DISABLED', 0, 0, 'UNQUALIFIED'),
    ('keungliang', 'DISABLED', 'DISABLED', 0, 0, 'UNQUALIFIED'),
    ('4router', 'DISABLED', 'DISABLED', 0, 0, 'UNQUALIFIED');

COMMENT ON TABLE travel_relay_endpoint_state IS 'v0.9.4.2 non-secret Writer endpoint capacity, qualification, circuit and weighted-selection authority';
COMMENT ON COLUMN travel_relay_endpoint_state.state_generation IS
    'Monotonic fence so stale probe completion cannot overwrite a newer generation';
COMMENT ON COLUMN travel_relay_endpoint_state.recovery_origin_circuit_state IS
    'OPEN or QUARANTINED origin restored when a HALF_OPEN lease expires';
COMMENT ON COLUMN travel_relay_endpoint_state.probe_kind IS
    'automatic organic trial or manual synthetic recover, never logged as a secret';
COMMENT ON COLUMN travel_relay_endpoint_state.active_operation_id IS
    'Manual recover operation owning the in-flight probe';
COMMENT ON COLUMN travel_relay_endpoint_state.manual_retry_not_before IS
    'Earliest next manual recover after a failed manual probe';
COMMENT ON TABLE travel_writer_dispatch IS 'v0.9.4.2 prompt-free durable Writer dispatch and capacity lease ledger';

CREATE TABLE travel_role_relay_endpoint_state (
    role                            VARCHAR(16) NOT NULL
        CONSTRAINT travel_role_relay_endpoint_state_role_check
        CHECK (role IN ('writer', 'review', 'grouping', 'selector', 'intent', 'extract', 'route_planner')),
    endpoint_name                   VARCHAR(64) NOT NULL,
    provider                        VARCHAR(64) NOT NULL DEFAULT 'unknown',
    failure_domain                  VARCHAR(128) NOT NULL DEFAULT '',
    configured                      BOOLEAN NOT NULL DEFAULT TRUE,
    participation                   VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'
        CHECK (participation IN ('ACTIVE', 'CANARY', 'DISABLED')),
    qualification_state             VARCHAR(16) NOT NULL DEFAULT 'QUALIFIED'
        CHECK (qualification_state IN ('QUALIFIED', 'UNQUALIFIED')),
    circuit_state                   VARCHAR(16) NOT NULL DEFAULT 'CLOSED'
        CHECK (circuit_state IN ('CLOSED', 'OPEN', 'HALF_OPEN', 'QUARANTINED')),
    configuration_fingerprint       VARCHAR(64) NOT NULL DEFAULT '',
    consecutive_transient_failures  SMALLINT NOT NULL DEFAULT 0
        CHECK (consecutive_transient_failures >= 0),
    cooldown_level                  SMALLINT NOT NULL DEFAULT 0
        CHECK (cooldown_level BETWEEN 0 AND 2),
    cooldown_until                  TIMESTAMPTZ,
    manual_retry_level              SMALLINT NOT NULL DEFAULT 0
        CHECK (manual_retry_level BETWEEN 0 AND 2),
    manual_retry_not_before         TIMESTAMPTZ,
    probe_kind                      VARCHAR(16)
        CHECK (probe_kind IS NULL OR probe_kind IN ('automatic', 'manual')),
    probe_owner                     VARCHAR(128),
    probe_lease_expires_at          TIMESTAMPTZ,
    recovery_origin_circuit_state   VARCHAR(16)
        CHECK (
            recovery_origin_circuit_state IS NULL
            OR recovery_origin_circuit_state IN ('OPEN', 'QUARANTINED')
        ),
    active_operation_id             UUID,
    state_generation                BIGINT NOT NULL DEFAULT 0
        CHECK (state_generation >= 0),
    last_success_at                 TIMESTAMPTZ,
    last_failure_at                 TIMESTAMPTZ,
    last_failure_code               VARCHAR(80),
    last_probe_at                   TIMESTAMPTZ,
    last_probe_status               VARCHAR(16)
        CHECK (last_probe_status IS NULL OR last_probe_status IN ('SUCCESS', 'FAILED', 'RUNNING')),
    incident_id                     UUID,
    incident_scope                  VARCHAR(16),
    incident_domain                 VARCHAR(160),
    incident_started_at             TIMESTAMPTZ,
    incident_resolved_at            TIMESTAMPTZ,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (role, endpoint_name),
    CONSTRAINT ck_role_relay_half_open_metadata CHECK (
        (
            circuit_state = 'HALF_OPEN'
            AND probe_kind IN ('automatic', 'manual')
            AND probe_owner IS NOT NULL
            AND probe_lease_expires_at IS NOT NULL
            AND recovery_origin_circuit_state IN ('OPEN', 'QUARANTINED')
            AND (
                probe_kind <> 'manual'
                OR active_operation_id IS NOT NULL
            )
        )
        OR (
            circuit_state <> 'HALF_OPEN'
            AND probe_kind IS NULL
            AND probe_owner IS NULL
            AND probe_lease_expires_at IS NULL
            AND recovery_origin_circuit_state IS NULL
            AND active_operation_id IS NULL
        )
    )
);

CREATE UNIQUE INDEX uq_role_relay_single_half_open
    ON travel_role_relay_endpoint_state (role)
    WHERE circuit_state = 'HALF_OPEN';
CREATE INDEX idx_role_relay_probe_lease
    ON travel_role_relay_endpoint_state (role, probe_lease_expires_at)
    WHERE circuit_state = 'HALF_OPEN';
CREATE INDEX idx_role_relay_failure_domain_incident
    ON travel_role_relay_endpoint_state (failure_domain, incident_id)
    WHERE incident_id IS NOT NULL AND incident_resolved_at IS NULL;
CREATE INDEX idx_role_relay_incident_scope_domain
    ON travel_role_relay_endpoint_state (incident_scope, incident_domain)
    WHERE incident_id IS NOT NULL AND incident_resolved_at IS NULL;

CREATE TABLE travel_llm_endpoint_recovery_operation (
    operation_id        UUID PRIMARY KEY,
    role                VARCHAR(16) NOT NULL
        CONSTRAINT travel_llm_endpoint_recovery_operation_role_check
        CHECK (role IN ('writer', 'review', 'grouping', 'selector')),
    endpoint_name       VARCHAR(64) NOT NULL,
    lifecycle           VARCHAR(32) NOT NULL
        CHECK (lifecycle IN (
            'PENDING',
            'SUCCEEDED',
            'FAILED',
            'EXPIRED',
            'CONFIGURATION_CHANGED'
        )),
    http_status         SMALLINT
        CHECK (http_status IS NULL OR http_status BETWEEN 200 AND 599),
    error_code          VARCHAR(64),
    error_message       VARCHAR(200),
    retry_after         TIMESTAMPTZ,
    replay_json         JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    CONSTRAINT ck_llm_recovery_operation_terminal CHECK (
        (
            lifecycle = 'PENDING'
            AND finished_at IS NULL
            AND replay_json IS NULL
            AND http_status IS NULL
            AND error_code IS NULL
            AND error_message IS NULL
            AND retry_after IS NULL
        )
        OR (
            lifecycle <> 'PENDING'
            AND finished_at IS NOT NULL
            AND replay_json IS NOT NULL
            AND http_status IS NOT NULL
        )
    )
);

CREATE INDEX idx_llm_recovery_operation_target
    ON travel_llm_endpoint_recovery_operation (role, endpoint_name, created_at DESC);

COMMENT ON TABLE travel_role_relay_endpoint_state IS
    'v0.9.9.10 Review/Grouping circuit and recovery authority with no capacity, lease or Writer dispatch ledger';
COMMENT ON COLUMN travel_role_relay_endpoint_state.configuration_fingerprint IS
    'Non-secret configuration identity digest, never logged, audited or returned';
COMMENT ON TABLE travel_llm_endpoint_recovery_operation IS
    'Durable recover replay ledger keyed by operation_id, storing only closed safe results';
COMMENT ON COLUMN travel_llm_endpoint_recovery_operation.replay_json IS
    'Safe projection or error envelope, never a URL, key, fingerprint, prompt or provider body';



-- -----------------------------------------------------------
-- 18. v0.9.5.1 Canonical POI detail gallery
-- -----------------------------------------------------------

CREATE TABLE travel_canonical_place_image (
    image_id        BIGSERIAL PRIMARY KEY,
    place_id        BIGINT NOT NULL REFERENCES travel_canonical_place(place_id) ON DELETE CASCADE,
    asset_id        TEXT NOT NULL CHECK (asset_id ~ '^[0-9a-f]{16}$'),
    position        SMALLINT NOT NULL CHECK (position BETWEEN 1 AND 6),
    desktop_url     TEXT NOT NULL CHECK (desktop_url ~ '^https://assets\.kakarot8\.com/[^[:space:]]+\.webp$'),
    desktop_width   INTEGER NOT NULL CHECK (desktop_width > 0),
    desktop_height  INTEGER NOT NULL CHECK (desktop_height > 0),
    mobile_url      TEXT NOT NULL CHECK (mobile_url ~ '^https://assets\.kakarot8\.com/[^[:space:]]+\.webp$'),
    mobile_width    INTEGER NOT NULL CHECK (mobile_width > 0),
    mobile_height   INTEGER NOT NULL CHECK (mobile_height > 0),
    thumb_url       TEXT NOT NULL CHECK (thumb_url ~ '^https://assets\.kakarot8\.com/[^[:space:]]+\.webp$'),
    thumb_width     INTEGER NOT NULL CHECK (thumb_width > 0),
    thumb_height    INTEGER NOT NULL CHECK (thumb_height > 0),
    alt_text        TEXT NOT NULL CHECK (length(btrim(alt_text)) BETWEEN 1 AND 300),
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    import_batch_id TEXT NOT NULL CHECK (length(btrim(import_batch_id)) BETWEEN 1 AND 120),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_canonical_place_image_asset UNIQUE (place_id, asset_id),
    CONSTRAINT uq_canonical_place_image_position UNIQUE (place_id, position)
);

CREATE INDEX idx_canonical_place_image_active
    ON travel_canonical_place_image (place_id, position) WHERE active = TRUE;
CREATE INDEX idx_canonical_place_image_import_batch
    ON travel_canonical_place_image (import_batch_id);
CREATE TRIGGER trg_canonical_place_image_updated
    BEFORE UPDATE ON travel_canonical_place_image
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE FUNCTION fn_validate_canonical_place_gallery_count()
RETURNS TRIGGER AS $$
DECLARE
    target_place_id BIGINT;
    active_count INTEGER;
BEGIN
    FOR target_place_id IN
        SELECT DISTINCT candidate
        FROM unnest(ARRAY[
            CASE WHEN TG_OP <> 'INSERT' THEN OLD.place_id END,
            CASE WHEN TG_OP <> 'DELETE' THEN NEW.place_id END
        ]) AS candidates(candidate)
        WHERE candidate IS NOT NULL
    LOOP
        IF EXISTS (SELECT 1 FROM travel_canonical_place WHERE place_id = target_place_id) THEN
            SELECT COUNT(*) INTO active_count
            FROM travel_canonical_place_image
            WHERE place_id = target_place_id AND active = TRUE;
            IF active_count <> 0 AND active_count NOT BETWEEN 1 AND 6 THEN
                RAISE EXCEPTION
                    'active canonical gallery for place_id % must contain 1..6 images, got %',
                    target_place_id, active_count USING ERRCODE = '23514';
            END IF;
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_validate_canonical_place_gallery_count
    AFTER INSERT OR UPDATE OR DELETE ON travel_canonical_place_image
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_validate_canonical_place_gallery_count();

COMMENT ON TABLE travel_canonical_place_image IS
    'Public WebP variants for lazy Canonical POI detail galleries; no raw source URLs or private master keys';

-- D1 additions only; existing schema drift is intentionally retained.
ALTER TABLE travel_place ADD COLUMN IF NOT EXISTS merged_into_place_id BIGINT
    REFERENCES travel_place(id);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'travel_place'::regclass AND conname = 'ck_place_merge_self') THEN
        ALTER TABLE travel_place ADD CONSTRAINT ck_place_merge_self
            CHECK (merged_into_place_id IS NULL OR merged_into_place_id <> id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_place_merge_target ON travel_place(merged_into_place_id)
    WHERE merged_into_place_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS travel_place_merge_log (
    id BIGSERIAL PRIMARY KEY,
    batch_id TEXT NOT NULL,
    merge_kind TEXT NOT NULL CHECK (merge_kind = 'same_city_amap'),
    city TEXT NOT NULL,
    amap_poi_id TEXT NOT NULL CHECK (amap_poi_id <> ''),
    survivor_id BIGINT NOT NULL REFERENCES travel_place(id),
    loser_id BIGINT NOT NULL REFERENCES travel_place(id),
    status TEXT NOT NULL CHECK (status IN ('pending_review','applied','reverted','do_not_merge')),
    reason TEXT NOT NULL,
    before_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    moved_rows JSONB NOT NULL DEFAULT '{}'::jsonb,
    deleted_rows JSONB NOT NULL DEFAULT '{}'::jsonb,
    alias_intents JSONB NOT NULL DEFAULT '[]'::jsonb,
    materialized_alias_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    redirected_children JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_rewash_required BOOLEAN NOT NULL DEFAULT FALSE,
    applied_at TIMESTAMPTZ,
    reverted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (survivor_id <> loser_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_place_merge_applied
    ON travel_place_merge_log(merge_kind,loser_id) WHERE status = 'applied';
CREATE UNIQUE INDEX IF NOT EXISTS uq_place_merge_pending
    ON travel_place_merge_log(merge_kind,loser_id) WHERE status = 'pending_review';
