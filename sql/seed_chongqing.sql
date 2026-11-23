-- ============================================================
-- Yuntu - 重庆精选真实种子数据 (Chongqing Seed Data)
-- 包含：城市激活状态、25+ 核心经典地点/美食/文化点、住宿商圈
-- ============================================================

BEGIN;

-- 1. 激活重庆城市记录
INSERT INTO travel_city (canonical_name, status, active_confirmed_time, created_time, updated_time)
VALUES ('重庆', 'ACTIVE', NOW(), NOW(), NOW())
ON CONFLICT (canonical_name) DO UPDATE 
SET status = 'ACTIVE', active_confirmed_time = NOW();

-- 2. 住宿推荐商圈 (accommodation_area)
INSERT INTO travel_canonical_place (
    canonical_name, city, district, address, adcode, latitude, longitude,
    amap_poi_id, place_type, category_tags, source_type, trust_level,
    review_status, is_active, contextual_only, geo_status, base_priority
) VALUES
('解放碑(商圈)', '重庆', '渝中区', '渝中区民权路', '500103', 29.556728, 106.576880, 'B0FFGI0JVF', 'accommodation_area', '["商圈", "市中心", "地标"]'::jsonb, 'amap', 'trusted', 'auto_accepted', TRUE, TRUE, 'resolved', 100),
('观音桥商圈', '重庆', '江北区', '江北区观音桥步行街', '500105', 29.576104, 106.533919, 'B0FFHOSTJ8', 'accommodation_area', '["商圈", "繁华", "美食"]'::jsonb, 'amap', 'trusted', 'auto_accepted', TRUE, TRUE, 'resolved', 95),
('南坪商圈', '重庆', '南岸区', '南岸区江南大道', '500108', 29.525042, 106.567212, 'B0LUPR3PAF', 'accommodation_area', '["商圈", "交通便利"]'::jsonb, 'amap', 'trusted', 'auto_accepted', TRUE, TRUE, 'resolved', 90),
('沙坪坝商圈', '重庆', '沙坪坝区', '沙坪坝区小龙坎新街', '500106', 29.554318, 106.457829, 'B0FFH1Q73E', 'accommodation_area', '["商圈", "高校区"]'::jsonb, 'amap', 'trusted', 'auto_accepted', TRUE, TRUE, 'resolved', 85)
ON CONFLICT DO NOTHING;

-- 3. 核心精选 POI 地点
INSERT INTO travel_canonical_place (
    canonical_name, city, district, address, adcode, latitude, longitude,
    amap_poi_id, place_type, category_tags, typical_visit_minutes, typical_visit_source,
    source_type, trust_level, review_status, is_active, contextual_only, geo_status, base_priority
) VALUES
-- 渝中区 (核心母城)
('洪崖洞民俗风貌区', '重庆', '渝中区', '渝中区嘉陵江滨江路88号', '500103', 29.562914, 106.582236, 'B0FFG8Y2L6', 'attraction', '["夜景", "吊脚楼", "网红打卡", "必去"]'::jsonb, 90, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 100),
('解放碑步行街', '重庆', '渝中区', '渝中区民族路177号', '500103', 29.557218, 106.577132, 'B0FFG0N645', 'attraction', '["地标", "历史纪念碑", "购物", "步行街"]'::jsonb, 60, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 98),
('长江索道', '重庆', '渝中区', '渝中区新华路151号', '500103', 29.558312, 106.584321, 'B001783TNL', 'attraction', '["索道", "江景", "空中巴士", "地标"]'::jsonb, 45, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 95),
('山城步道', '重庆', '渝中区', '渝中区中兴路234号', '500103', 29.551829, 106.570129, 'B0FFH7Q1A9', 'attraction', '["Citywalk", "老重庆", "江景", "步道"]'::jsonb, 90, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 92),
('十八梯传统风貌区', '重庆', '渝中区', '渝中区中兴路1号', '500103', 29.552431, 106.573512, 'B0J1K9L0M1', 'attraction', '["老街", "历史风貌", "夜景", "小吃"]'::jsonb, 75, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 90),
('李子坝轻轨穿楼', '重庆', '渝中区', '渝中区李子坝正街39号', '500103', 29.553924, 106.539218, 'B0FFH8M4P2', 'photo_spot', '["轻轨穿楼", "魔幻地形", "拍照打卡"]'::jsonb, 40, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 95),
('戴家巷崖壁步道', '重庆', '渝中区', '渝中区戴家巷', '500103', 29.563412, 106.580219, 'B0J2K3L4M5', 'photo_spot', '["悬崖步道", "咖啡馆", "江景", "文艺"]'::jsonb, 60, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 88),
('白象居', '重庆', '渝中区', '渝中区白象街4号', '500103', 29.556213, 106.586129, 'B0FFJ7K9L2', 'photo_spot', '["魔幻居民楼", "索道同框", "机位打卡"]'::jsonb, 50, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 87),
('湖广会馆', '重庆', '渝中区', '渝中区长滨路芭蕉园1号', '500103', 29.555219, 106.589124, 'B001783T6H', 'museum', '["古建筑群", "移民文化", "黄色封火墙", "历史"]'::jsonb, 75, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 85),
('重庆中国三峡博物馆', '重庆', '渝中区', '渝中区人民路236号', '500103', 29.565412, 106.550219, 'B001783U2L', 'museum', '["国家一级博物馆", "巴渝文化", "三峡历史"]'::jsonb, 120, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 90),
('重庆市人民大礼堂', '重庆', '渝中区', '渝中区人民路173号', '500103', 29.564819, 106.551912, 'B001783TNM', 'attraction', '["传统民族宫殿建筑", "地标", "广场"]'::jsonb, 45, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 86),
('罗汉寺', '重庆', '渝中区', '渝中区罗汉寺街7号', '500103', 29.560124, 106.581291, 'B001783TO1', 'attraction', '["千年古刹", "疯狂的石头取景地", "闹市净土"]'::jsonb, 60, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 82),

-- 渝中区/江北区 美食代表
('八一好吃街', '重庆', '渝中区', '渝中区八一路177号', '500103', 29.556214, 106.576129, 'B0FFH9N2K1', 'market', '["小吃街", "酸辣粉", "山城小汤圆", "串串"]'::jsonb, 60, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 92),
('大井巷社区火锅', '重庆', '渝中区', '渝中区大井巷老居民楼下', '500103', 29.555812, 106.578192, 'B0J3K4L5M6', 'restaurant', '["老火锅", "社区美食", "地道市井"]'::jsonb, 90, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 85),
('杨记隆府(解放碑总店)', '重庆', '渝中区', '渝中区临江支路30号', '500103', 29.558912, 106.574129, 'B0FFGX78Y9', 'restaurant', '["江湖菜", "辣子鸡", "毛血旺", "特色餐饮"]'::jsonb, 90, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 88),

-- 沙坪坝区 / 九龙坡区 (文化与慢生活)
('磁器口古镇', '重庆', '沙坪坝区', '沙坪坝区磁南街1号', '500106', 29.582914, 106.448219, 'B001783TNN', 'attraction', '["千年古镇", "陈麻花", "嘉陵江畔", "民俗文化"]'::jsonb, 120, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 93),
('渣滓洞/白公馆', '重庆', '沙坪坝区', '沙坪坝区歌乐山下', '500106', 29.581219, 106.425129, 'B001783TO2', 'museum', '["红色历史", "爱国教育", "歌乐山"]'::jsonb, 90, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 80),
('鹅岭二厂文创公园', '重庆', '渝中区', '渝中区鹅岭正街1号', '500103', 29.550124, 106.538129, 'B0FFH9M2Q8', 'park', '["文创园区", "俯瞰江景", "从你的全世界路过", "文艺打卡"]'::jsonb, 90, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 90),
('交通茶馆', '重庆', '九龙坡区', '九龙坡区黄桷坪正街20号', '500107', 29.498214, 106.532129, 'B0FFG8M4X3', 'attraction', '["老茶馆", "盖碗茶", "市井风情", "年代感"]'::jsonb, 60, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 86),
('黄桷坪涂鸦艺术街', '重庆', '九龙坡区', '九龙坡区黄桷坪正街', '500107', 29.499124, 106.531219, 'B0FFG9N5Y1', 'photo_spot', '["当代艺术", "巨幅涂鸦", "川美老校区"]'::jsonb, 60, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 82),

-- 南岸区 / 江北区 (江景与夜景视角)
('弹子石老街', '重庆', '南岸区', '南岸区泰昌路69号', '500108', 29.566129, 106.599124, 'B0FFH8Q3M1', 'attraction', '["百年老街", "江海关", "绝美夜景", "两江交汇"]'::jsonb, 90, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 89),
('南山一棵树观景台', '重庆', '南岸区', '南岸区龙黄公路', '500108', 29.544129, 106.595124, 'B001783TNP', 'photo_spot', '["全景夜景", "俯瞰渝中半岛", "摄影胜地"]'::jsonb, 60, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 91),
('九街/观音桥夜市', '重庆', '江北区', '江北区北城天街', '500105', 29.578129, 106.538124, 'B0FFH7P2L9', 'market', '["年轻潮流", "夜生活", "酒吧街", "地道小吃"]'::jsonb, 90, 'manual', 'official', 'trusted', 'auto_accepted', TRUE, FALSE, 'resolved', 88)
ON CONFLICT DO NOTHING;

-- 4. 同步标准化地点表 (travel_place) 与聚合摘要表 (travel_place_summary)
INSERT INTO travel_place (
    city, name, normalized_name, place_type, address, longitude, latitude, tags
)
SELECT
    c.city,
    c.canonical_name,
    LOWER(REGEXP_REPLACE(c.canonical_name, '\s+', '', 'g')),
    CASE
        WHEN c.place_type IN ('attraction', 'restaurant', 'business_area', 'market', 'park', 'museum', 'photo_spot', 'hotel', 'other')
        THEN c.place_type
        WHEN c.place_type = 'accommodation_area' THEN 'business_area'
        ELSE 'other'
    END,
    c.address,
    ROUND(c.longitude::numeric, 7),
    ROUND(c.latitude::numeric, 7),
    c.category_tags
FROM travel_canonical_place AS c
WHERE c.city = '重庆'
ON CONFLICT (city, normalized_name) DO UPDATE
SET
    address = EXCLUDED.address,
    longitude = EXCLUDED.longitude,
    latitude = EXCLUDED.latitude,
    tags = EXCLUDED.tags;

INSERT INTO travel_place_summary (
    place_id, canonical_place_id, city, name, place_type, address, longitude, latitude,
    tags, mention_count_30d, positive_count_30d, negative_count_30d, source_count,
    latest_captured_time, rating, top_reasons, warnings, hot_score, quality_score, recommend_score
)
SELECT
    p.id AS place_id,
    c.place_id AS canonical_place_id,
    c.city,
    c.canonical_name AS name,
    p.place_type,
    c.address,
    p.longitude,
    p.latitude,
    c.category_tags AS tags,
    GREATEST(12, c.base_priority - 40) AS mention_count_30d,
    GREATEST(10, c.base_priority - 45) AS positive_count_30d,
    1 AS negative_count_30d,
    4 AS source_count,
    NOW() AS latest_captured_time,
    4.7 AS rating,
    CASE c.canonical_name
        WHEN '洪崖洞民俗风貌区' THEN '[{"reason": "依山就势的巴渝吊脚楼建筑群，傍晚亮灯后临江立面与千厮门大桥同框夜景极具辨识度"}, {"reason": "建议从11楼沧白路或1楼嘉陵江滨江路错峰观景"}]'::jsonb
        WHEN '解放碑步行街' THEN '[{"reason": "重庆母城核心地标与商业街区，去八一好吃街、十八梯和洪崖洞步行皆可串联"}]'::jsonb
        WHEN '长江索道' THEN '[{"reason": "横跨长江的空中交通体验，轿厢内可俯瞰渝中半岛与南岸江景"}, {"reason": "新华路北站排队较长时，可从上新街南站反向乘坐"}]'::jsonb
        WHEN '山城步道' THEN '[{"reason": "沿崖壁修筑的老城石阶步道，串联仁爱堂旧址与临江栈道，适合慢走看江景"}]'::jsonb
        WHEN '十八梯传统风貌区' THEN '[{"reason": "连接上下半城的传统石阶老街，保留了山城吊脚楼与街巷肌理"}]'::jsonb
        WHEN '李子坝轻轨穿楼' THEN '[{"reason": "单轨2号线列车穿过居民楼的立体交通景观，楼下设专门观景平台"}]'::jsonb
        WHEN '戴家巷崖壁步道' THEN '[{"reason": "紧邻洪崖洞的临江悬崖栈道，老城墙与江畔咖啡小店氛围松弛"}]'::jsonb
        WHEN '白象居' THEN '[{"reason": "24层无电梯老式山城居民楼，可拍到长江索道与东水门大桥同框画面"}]'::jsonb
        WHEN '湖广会馆' THEN '[{"reason": "临江明清古建筑群，黄色封火墙与戏楼雕花保存完好，展现湖广填四川移民历史"}]'::jsonb
        WHEN '重庆中国三峡博物馆' THEN '[{"reason": "馆藏壮丽三峡、远古巴渝与城市变迁文物，与人民大礼堂隔广场相望"}]'::jsonb
        WHEN '重庆市人民大礼堂' THEN '[{"reason": "传统宫殿式穹顶建筑代表，广场开阔适合拍摄对称建筑外观"}]'::jsonb
        WHEN '罗汉寺' THEN '[{"reason": "闹市高楼环抱中的宋代古刹，古摩崖佛像与现代都市反差鲜明"}]'::jsonb
        WHEN '八一好吃街' THEN '[{"reason": "解放碑旁人气小吃街，汇集酸辣粉、山城小汤圆、烤脑花等街头小吃"}]'::jsonb
        WHEN '大井巷社区火锅' THEN '[{"reason": "藏在老居民楼下的地道重庆牛油老火锅，锅底醇厚、市井烟火气足"}]'::jsonb
        WHEN '杨记隆府(解放碑总店)' THEN '[{"reason": "重庆代表性江湖菜馆，招牌辣子鸡、毛血旺分量扎实，适合多人聚餐"}]'::jsonb
        WHEN '磁器口古镇' THEN '[{"reason": "嘉陵江畔千年水陆码头古镇，青石板路两侧汇集陈麻花与传统茶铺"}]'::jsonb
        WHEN '渣滓洞/白公馆' THEN '[{"reason": "歌乐山麓近代历史旧址，林木幽深，适合结合红色历史慢步参观"}]'::jsonb
        WHEN '鹅岭二厂文创公园' THEN '[{"reason": "印钞老厂房改造的工业风文创园，山顶天台可俯瞰两江风光"}]'::jsonb
        WHEN '交通茶馆' THEN '[{"reason": "保留上世纪老重庆风貌的老茶馆，木桌长凳配盖碗茶极具年代感"}]'::jsonb
        WHEN '黄桷坪涂鸦艺术街' THEN '[{"reason": "川美老校区周边整条街楼宇绘满彩绘涂鸦，与交通茶馆相邻适合顺路打卡"}]'::jsonb
        WHEN '弹子石老街' THEN '[{"reason": "南岸临江坡地老街，傍晚在观景台可正面观赏朝天门两江交汇夜景"}]'::jsonb
        WHEN '南山一棵树观景台' THEN '[{"reason": "南山半山腰经典观景平台，视野开阔，可俯瞰渝中半岛万家灯火"}]'::jsonb
        WHEN '九街/观音桥夜市' THEN '[{"reason": "江北核心夜生活与美食街区，夜间餐饮与小酒馆热闹非凡"}]'::jsonb
        ELSE '[{"reason": "重庆本地热门打卡与顺路游览地点"}]'::jsonb
    END AS top_reasons,
    CASE c.canonical_name
        WHEN '洪崖洞民俗风貌区' THEN '[{"warning": "节假日夜间19:30-21:30人流密集，注意错峰出行"}]'::jsonb
        WHEN '长江索道' THEN '[{"warning": "旺季排队时间较长，建议提前关注现场候乘提示"}]'::jsonb
        WHEN '山城步道' THEN '[{"warning": "全程台阶较多，建议穿舒适防滑平底鞋"}]'::jsonb
        ELSE '[]'::jsonb
    END AS warnings,
    c.base_priority::numeric AS hot_score,
    8.8 AS quality_score,
    9.0 AS recommend_score
FROM travel_canonical_place AS c
JOIN travel_place AS p
  ON p.city = c.city
 AND p.normalized_name = LOWER(REGEXP_REPLACE(c.canonical_name, '\s+', '', 'g'))
WHERE c.city = '重庆'
  AND c.contextual_only = FALSE
ON CONFLICT (place_id) DO UPDATE
SET
    canonical_place_id = EXCLUDED.canonical_place_id,
    top_reasons = EXCLUDED.top_reasons,
    warnings = EXCLUDED.warnings,
    mention_count_30d = EXCLUDED.mention_count_30d,
    source_count = EXCLUDED.source_count,
    recommend_score = EXCLUDED.recommend_score;

-- 5. 灵感图鉴与地点详情图集 (travel_canonical_place_image)
INSERT INTO travel_canonical_place_image (
    place_id, asset_id, position,
    desktop_url, desktop_width, desktop_height,
    mobile_url, mobile_width, mobile_height,
    thumb_url, thumb_width, thumb_height,
    alt_text, active, import_batch_id
)
SELECT
    c.place_id,
    SUBSTRING(MD5('seed-cq-img-' || c.canonical_name) FROM 1 FOR 16) AS asset_id,
    1 AS position,
    img.url AS desktop_url,
    1600 AS desktop_width,
    1067 AS desktop_height,
    REPLACE(img.url, '.webp', '.mobile.webp') AS mobile_url,
    800 AS mobile_width,
    533 AS mobile_height,
    REPLACE(img.url, '.webp', '.mobile.webp') AS thumb_url,
    400 AS thumb_width,
    267 AS thumb_height,
    c.canonical_name || '实景图' AS alt_text,
    TRUE AS active,
    'seed_chongqing_v1' AS import_batch_id
FROM travel_canonical_place AS c
JOIN (
    VALUES
        ('洪崖洞民俗风貌区', 'https://assets.kakarot8.com/city-opt/chongqing/commons-58923778.webp'),
        ('解放碑步行街', 'https://assets.kakarot8.com/city-opt/chongqing/commons-77016559.webp'),
        ('长江索道', 'https://assets.kakarot8.com/city-opt/chongqing/commons-12364263.webp'),
        ('山城步道', 'https://assets.kakarot8.com/city-opt/chongqing/andrea-sun-pMnpZayQJFE-unsplash.webp'),
        ('十八梯传统风貌区', 'https://assets.kakarot8.com/city-opt/chongqing/albert-canite-vMM4_VA8ogw-unsplash.webp'),
        ('李子坝轻轨穿楼', 'https://assets.kakarot8.com/city-opt/chongqing/albert-canite-RG2YD21o81E-unsplash.webp'),
        ('戴家巷崖壁步道', 'https://assets.kakarot8.com/city-opt/chongqing/harrison-qi-E9dIbdSd7LU-unsplash.webp'),
        ('白象居', 'https://assets.kakarot8.com/city-opt/chongqing/zhang-qc-EyScjFlzvtg-unsplash.webp'),
        ('弹子石老街', 'https://assets.kakarot8.com/city-opt/chongqing/commons-77016559.webp'),
        ('南山一棵树观景台', 'https://assets.kakarot8.com/city-opt/chongqing/commons-58923778.webp'),
        ('鹅岭二厂文创公园', 'https://assets.kakarot8.com/city-opt/chongqing/andrea-sun-pMnpZayQJFE-unsplash.webp'),
        ('磁器口古镇', 'https://assets.kakarot8.com/city-opt/chongqing/albert-canite-vMM4_VA8ogw-unsplash.webp')
) AS img(place_name, url)
  ON img.place_name = c.canonical_name
WHERE c.city = '重庆'
ON CONFLICT (place_id, position) DO UPDATE
SET
    desktop_url = EXCLUDED.desktop_url,
    mobile_url = EXCLUDED.mobile_url,
    thumb_url = EXCLUDED.thumb_url,
    alt_text = EXCLUDED.alt_text,
    active = TRUE;

COMMIT;
