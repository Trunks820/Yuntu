"""Destinations API — public directory for supported cinematic cities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["destinations"])

# 开源版默认内置 5 座重点城市的验证 POI 与实景种子数据：重庆、北京、上海、成都、杭州
# 如需扩展更多城市，可向数据库 travel_canonical_place / travel_place_summary 导入数据并在此处激活
CITIES_DATA: list[dict[str, Any]] = [
    {
        "id": "chongqing",
        "name": "重庆",
        "en_name": "Chongqing",
        "iata": "CKG",
        "region": "西南",
        "coordinates": {"lat": 29.56301, "lng": 106.551557},
        "map_label_offset": {"x": 10, "y": 6},
        "tags": ["8D魔幻", "赛博朋克", "火锅之都", "江畔夜景"],
        "quality": {"canonical_pass": True, "evidence_pass": True, "last_check_at": None},
        "card_cover_url": "https://assets.kakarot8.com/city-opt/chongqing/commons-12364263.mobile.jpg",
        "background_image_url": "https://assets.kakarot8.com/city-opt/chongqing/commons-12364263.mobile.jpg",
        "reference_images": [],
    },
    {
        "id": "beijing",
        "name": "北京",
        "en_name": "Beijing",
        "iata": "PEK",
        "region": "华北",
        "coordinates": {"lat": 39.904202, "lng": 116.407394},
        "map_label_offset": {"x": 0, "y": -14},
        "tags": ["红墙古都", "胡同烟火", "皇家园林", "京味美食"],
        "quality": {"canonical_pass": True, "evidence_pass": True, "last_check_at": None},
        "card_cover_url": "https://assets.kakarot8.com/city-opt/beijing/commons-15912403.mobile.jpg",
        "background_image_url": "https://assets.kakarot8.com/city-opt/beijing/commons-15912403.mobile.jpg",
        "reference_images": [],
    },
    {
        "id": "shanghai",
        "name": "上海",
        "en_name": "Shanghai",
        "iata": "SHA",
        "region": "华东",
        "coordinates": {"lat": 31.230416, "lng": 121.473701},
        "map_label_offset": {"x": 8, "y": -2},
        "tags": ["梧桐街区", "外滩江风", "海派美术馆", "城市漫步"],
        "quality": {"canonical_pass": True, "evidence_pass": True, "last_check_at": None},
        "card_cover_url": "https://assets.kakarot8.com/city-opt/shanghai/commons-75799783.mobile.jpg",
        "background_image_url": "https://assets.kakarot8.com/city-opt/shanghai/commons-75799783.mobile.jpg",
        "reference_images": [],
    },
    {
        "id": "chengdu",
        "name": "成都",
        "en_name": "Chengdu",
        "iata": "CTU",
        "region": "西南",
        "coordinates": {"lat": 30.572269, "lng": 104.066541},
        "map_label_offset": {"x": -10, "y": -4},
        "tags": ["公园茶馆", "大熊猫基地", "川味夜市", "慢生活"],
        "quality": {"canonical_pass": True, "evidence_pass": True, "last_check_at": None},
        "card_cover_url": "https://assets.kakarot8.com/city-opt/chengdu/commons-109407045.mobile.jpg",
        "background_image_url": "https://assets.kakarot8.com/city-opt/chengdu/commons-109407045.mobile.jpg",
        "reference_images": [],
    },
    {
        "id": "hangzhou",
        "name": "杭州",
        "en_name": "Hangzhou",
        "iata": "HGH",
        "region": "华东",
        "coordinates": {"lat": 30.274085, "lng": 120.15507},
        "map_label_offset": {"x": 8, "y": 14},
        "tags": ["西湖山水", "九溪茶园", "江南古刹", "湖畔骑行"],
        "quality": {"canonical_pass": True, "evidence_pass": True, "last_check_at": None},
        "card_cover_url": "https://assets.kakarot8.com/city-opt/hangzhou/commons-49832170.mobile.jpg",
        "background_image_url": "https://assets.kakarot8.com/city-opt/hangzhou/commons-49832170.mobile.jpg",
        "reference_images": [],
    },
]


@router.get("/destinations")
@router.get("/api/destinations")
async def get_destinations() -> dict[str, Any]:
    return {
        "success": True,
        "destinations": CITIES_DATA,
        "total": len(CITIES_DATA),
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
