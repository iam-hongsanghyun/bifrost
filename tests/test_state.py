"""Shaping Ragnarok's responses for the State panel — pure functions, no transport."""

from __future__ import annotations

from bifrost import state

# ── model ────────────────────────────────────────────────────────────────────


def _meta(*sheets: tuple[str, str, int, list[str]]) -> dict:
    return {"version": 7, "filename": "m", "snapshotCount": 24,
            "sheets": [{"name": n, "kind": k, "rowCount": r, "columns": c} for n, k, r, c in sheets]}


MODEL = {
    "buses": [{"name": "n", "x": 127.0, "y": 37.8, "v_nom": 380, "carrier": "AC"},
              {"name": "s", "x": 128.6, "y": 35.2, "v_nom": 380, "carrier": "AC"}],
    "generators": [{"name": "wind", "bus": "n", "carrier": "wind", "p_nom": 500},
                   {"name": "gas", "bus": "s", "carrier": "gas", "p_nom": 1000},
                   {"name": "ghost", "bus": "nowhere", "carrier": "gas", "p_nom": 1}],
    "lines": [{"name": "n-s", "bus0": "n", "bus1": "s", "s_nom": 2000, "carrier": ""}],
    "carriers": [{"name": "gas", "co2_emissions": 0.18, "color": "#8c564b"}, {"name": "wind"}],
    "snapshots": [{"snapshot": "t0"}],
}
META = _meta(("snapshots", "static", 1, ["snapshot"]), ("buses", "static", 2, ["name", "x", "y"]),
             ("generators", "static", 3, ["name", "bus", "carrier", "p_nom"]),
             ("lines", "static", 1, ["name", "bus0", "bus1", "s_nom"]), ("carriers", "static", 2, ["name"]),
             ("loads-p_set", "series", 24, ["snapshot", "l"]))


def test_empty_session_is_reported_as_not_loaded() -> None:
    out = state.shape_model({}, None)
    assert out["loaded"] is False and out["counts"] == []


def test_model_counts_roles_and_series_sheets() -> None:
    out = state.shape_model(META, MODEL)
    roles = {c["sheet"]: c["role"] for c in out["counts"]}
    assert roles == {"buses": "bus", "generators": "attached", "lines": "branch", "carriers": "other"}
    assert "snapshots" not in roles
    assert out["series_sheets"][0]["name"] == "loads-p_set"


def test_topology_attaches_assets_and_counts_orphans() -> None:
    topo = state.shape_model(META, MODEL)["topology"]
    by_bus = {b["name"]: b for b in topo["buses"]}
    assert [a["name"] for a in by_bus["n"]["assets"]] == ["wind"]
    assert by_bus["s"]["assets"][0]["p_nom_mw"] == 1000.0
    assert topo["unattached"] == 1  # "ghost" points at a bus that does not exist
    assert topo["branches"][0]["capacity_mw"] == 2000.0 and topo["has_coordinates"] is True


def test_buses_all_at_the_origin_are_not_a_map() -> None:
    model = {**MODEL, "buses": [{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 0, "y": 0}]}
    assert state.shape_model(META, model)["topology"]["has_coordinates"] is False


def test_carriers_get_asset_counts_and_unit_named_emissions() -> None:
    carriers = {c["name"]: c for c in state.shape_model(META, MODEL)["carriers"]}
    assert carriers["gas"]["asset_count"] == 2 and carriers["gas"]["co2_emissions_t_per_mwh_th"] == 0.18


# ── tables ───────────────────────────────────────────────────────────────────

SHEET = {"name": "generators", "kind": "static", "columns": ["name", "bus", "p_nom", "control"],
         "rows": [{"name": "b", "bus": "x", "p_nom": 10, "control": "PQ"},
                  {"name": "a", "bus": "y", "p_nom": 200, "control": "PQ"},
                  {"name": "c", "bus": "x", "p_nom": None, "control": "PQ"}]}


def test_table_sorts_numbers_as_numbers_and_puts_gaps_last_either_way() -> None:
    up = state.table_page(SHEET, offset=0, limit=10, q=None, sort="p_nom", descending=False)
    down = state.table_page(SHEET, offset=0, limit=10, q=None, sort="p_nom", descending=True)
    assert [r["name"] for r in up["items"]] == ["b", "a", "c"]   # 10 < 200, not "10" < "200" by luck
    assert [r["name"] for r in down["items"]] == ["a", "b", "c"]  # the gap stays last


def test_table_filters_pages_and_reports_the_total() -> None:
    out = state.table_page(SHEET, offset=1, limit=1, q="X", sort=None, descending=False)
    assert out["total"] == 2 and [r["name"] for r in out["items"]] == ["c"]


def test_key_columns_are_the_first_plus_those_that_vary() -> None:
    out = state.table_page(SHEET, offset=0, limit=10, q=None, sort=None, descending=False)
    assert out["key_columns"] == ["name", "bus", "p_nom"]  # "control" is PQ everywhere


# ── journal, queue, runs, kpis ───────────────────────────────────────────────


def test_journal_pages_back_by_the_oldest_id_and_stops_at_the_end() -> None:
    page = {"version": 9, "entries": [{"id": 9}, {"id": 8}]}
    assert state.shape_journal(page, 2)["next_before"] == 8
    assert state.shape_journal(page, 50)["next_before"] is None


def test_queue_reports_elapsed_for_running_and_finished_jobs() -> None:
    jobs = {"jobs": [
        {"id": "1", "status": "running", "startedAt": "2030-01-01T00:00:00+00:00", "finishedAt": None},
        {"id": "2", "status": "done", "startedAt": "2030-01-01T00:00:00+00:00",
         "finishedAt": "2030-01-01T00:00:12+00:00"},
        {"id": "3", "status": "queued", "startedAt": None, "finishedAt": None}],
        "concurrency": 1, "maxConcurrency": 8}
    out = state.shape_queue(jobs, "2030-01-01T00:00:30+00:00")
    assert [j["elapsed_s"] for j in out["items"]] == [30.0, 12.0, None]
    assert out["active"] == 2 and out["total"] == 3


def test_kpis_become_numbers_with_units() -> None:
    kpis = state.shape_kpis({"totalCost": "$328,875", "avgPrice": 56.6, "loadFactor": 0.709,
                             "emissions": "1 ktCO2e", "currencySymbol": "$", "snapshotWeightHours": 1.0,
                             "somethingNew": 3})
    by = {k["key"]: k for k in kpis}
    assert by["totalCost"]["value"] == 328875.0 and by["totalCost"]["unit"] == "$"
    assert by["avgPrice"]["unit"] == "$/MWh"
    assert abs(by["loadFactor"]["value"] - 70.9) < 1e-9 and by["loadFactor"]["unit"] == "%"
    assert by["emissions"]["value"] is None and by["emissions"]["text"] == "1 ktCO2e"
    assert "snapshotWeightHours" not in by and by["somethingNew"]["label"] == "Something new"


# ── dashboard ────────────────────────────────────────────────────────────────

TS = ["2030-06-01T00:00:00", "2030-06-01T01:00:00", "2030-06-01T02:00:00"]


def _analytics() -> dict:
    return {"label": "Run", "savedAt": "s", "origin": "solve", "result": {
        "runMeta": {"snapshotCount": 3},
        "dispatchSeries": [
            {"timestamp": TS[0], "period": None, "values": {"wind": 100.0}, "total": 250.0},
            {"timestamp": TS[1], "period": None, "values": {"wind": 50.0, "gas": 150.0}, "total": 200.0},
            {"timestamp": TS[2], "period": None, "values": {"gas": 90.0}, "total": 90.0}],
        "nodalPriceSeries": [{"timestamp": t.replace("T", " "), "values": {"n": float(i), "s": 10.0 + i}}
                             for i, t in enumerate(TS)],
        "systemPriceSeries": [{"timestamp": t, "value": 5.0 + i} for i, t in enumerate(TS)],
        "curtailmentSeries": [{"timestamp": t, "values": {} if i else {"wind": 7.0}} for i, t in enumerate(TS)],
        "storageSeries": [{"timestamp": t, "charge": 0.0, "discharge": 0.0, "state": 0.0} for t in TS],
        "storageSocSeries": [], "systemEmissionsSeries": [{"timestamp": t, "value": 1.0 * i} for i, t in enumerate(TS)],
        "carrierMix": [{"label": "gas", "value": 240.0, "color": "#8c564b"}, {"label": "wind", "value": 150.0}],
        "costBreakdown": [{"label": "Fuel cost", "value": 10}, {"label": "Carbon cost", "value": 0}],
        "lineLoading": [{"label": "n-s", "value": 12.5}],
        "expansionResults": [],
        "outputs": {"static": {"generators": {"wind": {"p_nom_opt": 800.0}, "gas": {"p_nom_opt": 1000.0}}}},
        "emissionsBreakdown": {"byGenerator": [
            {"name": "gas", "carrier": "gas", "bus": "s", "energy_mwh": 240.0, "emissions_tco2": 43.0}]}}}


GENS = [{"name": "wind", "bus": "n", "carrier": "wind", "p_nom": 500},
        {"name": "gas", "bus": "s", "carrier": "gas", "p_nom": 1000}]


def dash(**over):
    args = dict(name="r", analytics=_analytics(), kpis={"avgPrice": 5.0, "currencySymbol": "$"},
                generators=GENS, storage_units=[], generator_p=None, bus=None, carrier=None, period=None)
    args.update(over)
    return state.build_dashboard(**args)


def test_an_idle_carrier_is_zero_not_missing() -> None:
    d = dash()
    by = {s["name"]: s["values"] for s in d["dispatch_mw"]["series"]}
    assert by["gas"] == [0.0, 150.0, 90.0] and by["wind"] == [100.0, 50.0, 0.0]
    assert d["x"][0] == "2030-06-01 00:00" and d["snapshot_index"] == [0, 1, 2]
    assert d["dispatch_mw"]["load_mw"] == [250.0, 200.0, 90.0]
    assert next(s for s in d["dispatch_mw"]["series"] if s["name"] == "gas")["color"] == "#8c564b"


def test_prices_default_to_system_plus_each_bus_and_narrow_to_one() -> None:
    assert [s["name"] for s in dash()["price"]["series"]] == ["system (load-weighted)", "n", "s"]
    only = dash(bus="s")["price"]["series"]
    assert [s["name"] for s in only] == ["s"] and only[0]["values"] == [10.0, 11.0, 12.0]


def test_carrier_filter_narrows_dispatch_curtailment_and_capacity() -> None:
    d = dash(carrier="wind")
    assert [s["name"] for s in d["dispatch_mw"]["series"]] == ["wind"]
    assert d["capacity_mw"]["categories"] == ["wind"]
    assert d["capacity_mw"]["series"][1]["values"] == [800.0]  # optimal, from p_nom_opt
    assert d["curtailment_mw"]["series"][0]["values"] == [7.0, 0.0, 0.0]
    assert d["carriers"] == ["gas", "wind"], "the selector must keep listing every carrier"


def test_bus_filter_resums_per_asset_output_over_that_bus() -> None:
    gen_p = {"indexCol": "snapshot", "columns": ["snapshot", "wind", "gas"],
             "rows": [{"snapshot": t, "wind": 10.0 * (i + 1), "gas": 99.0} for i, t in enumerate(TS)]}
    d = dash(bus="n", generator_p=gen_p)
    assert [(s["name"], s["values"]) for s in d["dispatch_mw"]["series"]] == [("wind", [10.0, 20.0, 30.0])]
    assert d["dispatch_mw"]["load_mw"] is None  # system load is not this bus's load
    assert d["capacity_mw"]["categories"] == ["wind"] and d["bus_filter_note"]


def test_period_filter_keeps_only_that_period_and_maps_back_to_snapshot_index() -> None:
    a = _analytics()
    for i, row in enumerate(a["result"]["dispatchSeries"]):
        row["period"] = 2030 if i < 2 else 2040
    d = dash(analytics=a, period="2040")
    assert d["periods"] == ["2030", "2040"] and d["x"] == ["2030-06-01 02:00"] and d["snapshot_index"] == [2]
    assert d["price"]["series"][0]["values"] == [7.0]


def test_stride_is_reported_when_ragnarok_stored_every_nth_snapshot() -> None:
    a = _analytics()
    a["result"]["runMeta"]["snapshotCount"] = 6
    d = dash(analytics=a)
    assert d["stride"] == 2 and d["snapshot_index"] == [0, 2, 4]


# ── snapshot drill-in and comparison ─────────────────────────────────────────


def test_snapshot_groups_assets_by_carrier_and_bus_and_adds_up() -> None:
    gen_p = {"indexCol": "snapshot", "rows": [{"snapshot": TS[1], "wind": 50.0, "gas": 150.0}]}
    price = {"indexCol": "snapshot", "rows": [{"snapshot": TS[1], "n": 1.0, "s": 2.0}]}
    load = {"indexCol": "snapshot", "rows": [{"snapshot": TS[1], "l1": 120.0, "l2": 80.0}]}
    out = state.build_snapshot(index=1, label="x", generator_p=gen_p, load_p=load, price=price, flows=None,
                               generators=GENS, colours={"gas": "#8c564b"}, bus=None)
    assert [(c["name"], c["p_mw"]) for c in out["by_carrier"]] == [("gas", 150.0), ("wind", 50.0)]
    assert abs(sum(c["p_mw"] for c in out["by_carrier"]) - sum(a["p_mw"] for a in out["by_asset"])) < 1e-9
    assert out["load_mw"] == 200.0
    assert {b["bus"]: b["price"] for b in out["by_bus"]} == {"n": 1.0, "s": 2.0}
    only_n = state.build_snapshot(index=1, label="x", generator_p=gen_p, load_p=None, price=price, flows=None,
                                  generators=GENS, colours={}, bus="n")
    assert [a["name"] for a in only_n["by_asset"]] == ["wind"]


def test_comparison_deltas_are_against_the_first_run() -> None:
    def entry(name: str, cost: str, avg: float, mix: dict[str, float]) -> dict:
        a = _analytics()
        a["result"]["carrierMix"] = [{"label": k, "value": v} for k, v in mix.items()]
        return {"name": name, "analytics": a, "kpis": {"totalCost": cost, "avgPrice": avg, "currencySymbol": "$"}}

    out = state.build_comparison([entry("a", "$100", 50.0, {"gas": 10.0}), entry("b", "$150", 40.0, {"gas": 4.0, "wind": 6.0})])
    kpi = {k["key"]: k for k in out["kpis"]}
    assert kpi["totalCost"]["delta"] == [0.0, 50.0] and kpi["totalCost"]["delta_pct"] == [0.0, 50.0]
    assert kpi["avgPrice"]["delta_pct"][1] == -20.0
    assert out["generation_mwh"]["categories"] == ["gas", "wind"]
    assert [s["values"] for s in out["generation_mwh"]["series"]] == [[10.0, 0.0], [4.0, 6.0]]
