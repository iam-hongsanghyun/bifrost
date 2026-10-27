"""``show_chart`` -> ``chart.show`` payload: what used to go wrong silently."""

from __future__ import annotations

import math

from bifrost.charts import build_chart_payload, thin_indices

LIMITS = {"max_points": 10_000, "max_cells": 250_000}


def build(args: dict, **over):
    return build_chart_payload(args, conversation_id="c1", **{**LIMITS, **over})


def test_a_year_of_hourly_points_is_not_cut_to_the_first_2000() -> None:
    n = 8760
    payload = build({"title": "t", "kind": "line", "x": [str(i) for i in range(n)],
                     "series": [{"name": "a", "values": list(range(n))}]})
    assert isinstance(payload, dict)
    assert len(payload["x"]) == n and payload["series"][0]["values"][-1] == n - 1
    assert "thinnedFrom" not in payload


def test_a_missing_value_keeps_its_place_instead_of_shifting_the_series() -> None:
    payload = build({"title": "t", "kind": "line", "x": ["a", "b", "c", "d"],
                     "series": [{"name": "s", "values": [1, None, "n/a", 4]}]})
    assert isinstance(payload, dict)
    # The old code dropped the non-numbers first, so 4 landed under label "c".
    assert payload["series"][0]["values"] == [1.0, None, None, 4.0]


def test_a_short_series_is_padded_with_gaps_not_stretched() -> None:
    payload = build({"title": "t", "kind": "line", "x": ["a", "b", "c"],
                     "series": [{"name": "s", "values": [1, 2]}]})
    assert payload["series"][0]["values"] == [1.0, 2.0, None]  # type: ignore[index]


def test_booleans_and_non_finite_numbers_are_gaps() -> None:
    payload = build({"title": "t", "kind": "line", "x": list("abcd"),
                     "series": [{"name": "s", "values": [True, math.nan, math.inf, 5]}]})
    assert payload["series"][0]["values"] == [None, None, None, 5.0]  # type: ignore[index]


def test_a_series_with_no_numbers_is_dropped_and_none_left_is_refused() -> None:
    kept = build({"title": "t", "kind": "line", "x": ["a"], "series": [
        {"name": "empty", "values": [None]}, {"name": "ok", "values": [1]}]})
    assert [s["name"] for s in kept["series"]] == ["ok"]  # type: ignore[index]
    refused = build({"title": "t", "kind": "line", "x": ["a"], "series": [{"name": "e", "values": []}]})
    assert isinstance(refused, str) and refused.startswith("Not drawn")
    assert isinstance(build({"title": "t", "kind": "line", "x": [], "series": []}), str)


def test_oversize_chart_is_thinned_with_shared_indices_and_keeps_the_peak() -> None:
    n = 20_000
    spike = 12_345
    a = [1.0] * n
    b = [2.0] * n
    a[spike] = 900.0  # the scarcity hour
    payload = build({"title": "t", "kind": "line", "x": [str(i) for i in range(n)],
                     "series": [{"name": "a", "values": a}, {"name": "b", "values": b}]},
                    max_points=1000)
    assert isinstance(payload, dict)
    xs = [int(v) for v in payload["x"]]
    assert payload["thinnedFrom"] == n and len(xs) <= 1200 and xs[0] == 0 and xs[-1] == n - 1
    assert spike in xs, "thinning must not lose the peak"
    # Alignment: the value under each kept label is that label's own value.
    for label, va, vb in zip(xs, payload["series"][0]["values"], payload["series"][1]["values"]):
        assert va == a[label] and vb == b[label]


def test_cell_budget_applies_across_series() -> None:
    n, k = 8760, 50  # 438,000 cells
    payload = build({"title": "t", "kind": "line", "x": [str(i) for i in range(n)],
                     "series": [{"name": f"s{j}", "values": [float(j)] * n} for j in range(k)]})
    assert isinstance(payload, dict)
    assert len(payload["x"]) * k <= LIMITS["max_cells"] * 1.05 and payload["thinnedFrom"] == n


def test_thin_indices_is_identity_when_it_fits() -> None:
    assert thin_indices([[1.0, 2.0, 3.0]], 10) == [0, 1, 2]


def test_heatmap_is_refused_rather_than_reinterpreted_when_too_big() -> None:
    msg = build({"title": "t", "kind": "heatmap", "x": [str(i) for i in range(24)],
                 "series": [{"name": f"d{j}", "values": [1.0] * 24} for j in range(40)]}, max_cells=500)
    assert isinstance(msg, str) and "heatmap" in msg


def test_axis_kind_stack_and_colour_pass_through_with_validation() -> None:
    payload = build({"title": "t", "kind": "area", "stacked": True, "unit": "MW", "unitRight": "$/MWh",
                     "x": ["a", "b"], "series": [
                         {"name": "gas", "values": [1, 2], "color": "#8c564b"},
                         {"name": "bad", "values": [1, 2], "color": "url(javascript:alert(1))"},
                         {"name": "price", "values": [5, 6], "axis": "RIGHT", "kind": "line"}]})
    assert isinstance(payload, dict)
    gas, bad, price = payload["series"]
    assert gas["color"] == "#8c564b" and "color" not in bad
    assert price["axis"] == "right" and price["kind"] == "line"
    assert payload["chartKind"] == "area" and payload["stacked"] is True and payload["unitRight"] == "$/MWh"
    assert payload["conversationId"] == "c1" and len(payload["chartId"]) == 12
