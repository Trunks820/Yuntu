"""v0.9.7 cluster-first day ordering: kill the A->B->A geographic sandwich."""

import sys

sys.path.insert(0, ".")

from src.agents.route_planning import _order_day_groups_cluster_first
from src.agents.schema import CandidatePlace, RouteDayGroup


def make_place(place_id, lat, lng, adcode):
    return CandidatePlace(
        place_id=place_id,
        name=f"poi-{place_id}",
        place_type="attraction",
        latitude=lat,
        longitude=lng,
        adcode=adcode,
    )


def make_day(base_id, center, adcode):
    lat, lng = center
    places = [
        make_place(base_id, lat, lng, adcode),
        make_place(base_id + 1, lat + 0.004, lng + 0.004, adcode),
    ]
    return RouteDayGroup(day=0, adcode=adcode, places=places)


# 阳朔县 (450321) vs 桂林市区 (450302), ~65 km apart; two 阳朔 days ~1 km apart.
YS1 = (24.7780, 110.4970)
YS2 = (24.7860, 110.5050)
GL = (25.3120, 110.2900)


def adcodes(day_groups):
    return [day_group.adcode for day_group in day_groups]


def is_contiguous(seq):
    runs = []
    for value in seq:
        if not runs or runs[-1] != value:
            runs.append(value)
    # Contiguous iff no area reappears after a different one (one run each).
    return len(runs) == len(set(runs))


def test_contiguous_input_stays_contiguous():
    days = [
        make_day(100, YS1, "450321"),
        make_day(200, YS2, "450321"),
        make_day(300, GL, "450302"),
    ]
    result = _order_day_groups_cluster_first(days)
    assert len(result) == 3
    assert is_contiguous(adcodes(result)), adcodes(result)


def test_sandwich_input_is_repaired():
    # Builder-emitted sandwich: 阳朔, 桂林市区, 阳朔.
    days = [
        make_day(100, YS1, "450321"),
        make_day(300, GL, "450302"),
        make_day(200, YS2, "450321"),
    ]
    result = _order_day_groups_cluster_first(days)
    assert is_contiguous(adcodes(result)), adcodes(result)
    assert adcodes(result).count("450302") == 1


def test_single_area_returns_unchanged():
    days = [
        make_day(100, YS1, "450321"),
        make_day(200, YS2, "450321"),
        make_day(300, (24.79, 110.51), "450321"),
    ]
    result = _order_day_groups_cluster_first(days)
    assert [id(day) for day in result] == [id(day) for day in days]


def test_two_days_untouched():
    days = [make_day(100, YS1, "450321"), make_day(300, GL, "450302")]
    result = _order_day_groups_cluster_first(days)
    assert [id(day) for day in result] == [id(day) for day in days]


def test_missing_adcode_uses_geo_fallback():
    # No adcode: two nearby days + one far day must still avoid a sandwich.
    days = [
        make_day(100, YS1, None),
        make_day(300, GL, None),
        make_day(200, YS2, None),
    ]
    result = _order_day_groups_cluster_first(days)
    zones = ["far" if day.places[0].latitude > 25 else "near" for day in result]
    assert is_contiguous(zones), zones


def test_three_areas_single_sweep_no_backtrack():
    area_a = make_day(100, (24.0, 110.0), "A")
    area_b = make_day(200, (25.0, 110.0), "B")
    area_c = make_day(300, (26.0, 110.0), "C")
    result = _order_day_groups_cluster_first([area_c, area_a, area_b])
    seq = adcodes(result)
    assert seq in (["A", "B", "C"], ["C", "B", "A"]), seq


