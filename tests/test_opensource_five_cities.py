"""Verify open-source five-city seed, API directory, and slimmed static catalogs."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.destinations import router as destinations_router
from src.cost_reference.catalog import load_reference_catalog, load_verified_facts
from src.export.city_photo import CityPhotoResolver

REPO_ROOT = Path(__file__).resolve().parents[1]
FIVE_CITIES = {"重庆", "北京", "上海", "成都", "杭州"}


def test_destinations_api_exposes_five_flagship_cities():
    app = FastAPI()
    app.include_router(destinations_router)
    client = TestClient(app)

    for path in ("/destinations", "/api/destinations"):
        resp = client.get(path)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["success"] is True
        assert payload["total"] == 5
        names = {item["name"] for item in payload["destinations"]}
        assert names == FIVE_CITIES
        for item in payload["destinations"]:
            assert item["coordinates"]["lat"]
            assert item["coordinates"]["lng"]
            assert item["card_cover_url"].startswith("https://")
            assert item["background_image_url"].startswith("https://")


def test_sql_seeds_cover_all_five_cities_and_are_wired_in_init_db():
    chongqing_sql = (REPO_ROOT / "sql" / "seed_chongqing.sql").read_text(encoding="utf-8")
    cities_sql = (REPO_ROOT / "sql" / "seed_cities.sql").read_text(encoding="utf-8")
    init_db_py = (REPO_ROOT / "scripts" / "init_db.py").read_text(encoding="utf-8")

    assert "seed_chongqing.sql" in init_db_py
    assert "seed_cities.sql" in init_db_py
    assert "'重庆'" in chongqing_sql
    for city in ("'北京'", "'上海'", "'成都'", "'杭州'"):
        assert city in cities_sql


def test_static_catalogs_are_slimmed_to_five_cities():
    facts = load_verified_facts()
    assert {item.city for item in facts.admission} == FIVE_CITIES
    assert {item.city for item in facts.meal_city_reference.cities} == FIVE_CITIES

    catalog = load_reference_catalog()
    assert {entry.city for entry in catalog.accommodation} == FIVE_CITIES
    assert {entry.city for entry in catalog.admission} == FIVE_CITIES
    assert {entry.city for entry in catalog.meals} == FIVE_CITIES
    assert {entry.city for entry in catalog.local_transport} == FIVE_CITIES
    for route in catalog.intercity:
        assert route.from_city in FIVE_CITIES
        assert route.to_city in FIVE_CITIES

    photo_catalog = json.loads(
        (REPO_ROOT / "src" / "export" / "assets" / "city_photo_catalog.json").read_text(encoding="utf-8")
    )
    assert {item["city"] for item in photo_catalog["photos"]} == FIVE_CITIES
    assert set(CityPhotoResolver()._entries) == FIVE_CITIES
