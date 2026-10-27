"""Shape Ragnarok's model, journal, queue and run results for the State panel.

Ragnarok's routes return what its own UI wants: row objects per snapshot, string
KPIs like ``"$328,875"``, sparse dicts where an idle carrier is simply absent. The
State panel wants columns it can hand straight to a chart, numbers it can compare,
and an explicit rule for "missing". Those conversions are here, as pure functions
over already-fetched JSON, so they are tested without a transport and the router
stays a list of thin fetches.

Conventions, decided once:

* a carrier with no entry in a snapshot generated **0 MW** (Ragnarok omits idle
  carriers) — filled with ``0.0``;
* a value that cannot be a number is ``None`` and travels as JSON ``null``;
* every list response is ``{"items": [...], "total": n}``;
* units are in key names (``capacity_mw``, ``co2_emissions_t_per_mwh_th``).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: Sheets that describe the study, not components of the network.
_NOT_COMPONENTS = {"snapshots", "network"}

#: Components whose installed vs optimised capacity the dashboard compares.
_CAPACITY_SHEETS = ("generators", "storage_units")

_MAX_ASSETS_PER_BUS = 200
_MAX_PRICE_BUS_LINES = 12


# ── small helpers ────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _label(sheet: str) -> str:
    return sheet.replace("_", " ")


def _ts(value: Any) -> str:
    """``2030-06-01T00:00:00`` / ``2030-06-01 00:00:00`` -> ``2030-06-01 00:00``."""
    text = str(value or "").replace("T", " ")
    return text[:16] if len(text) >= 16 else text


def _parse_amount(text: Any) -> float | None:
    """``"$328,875"`` -> 328875.0. ``None`` when there is no plain number."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"\s*[^\d\-.]*(-?[\d,]*\.?\d+)\s*", text)
    return float(match.group(1).replace(",", "")) if match else None


# ── model ────────────────────────────────────────────────────────────────────


def sheet_role(name: str, columns: Iterable[str]) -> str:
    """How a static sheet relates to the network graph: ``bus``, ``branch`` (joins
    two buses), ``attached`` (hangs off one bus) or ``other``."""
    cols = set(columns)
    if name == "buses":
        return "bus"
    if {"bus0", "bus1"} <= cols:
        return "branch"
    if "bus" in cols:
        return "attached"
    return "other"


def shape_model(
    meta: dict[str, Any], model: dict[str, list[dict[str, Any]]] | None
) -> dict[str, Any]:
    """The Model section: counts, carriers, and the graph for the topology map.

    Args:
        meta: ``GET /api/session/meta`` (``{}`` when nothing is loaded).
        model: ``GET /api/session/model/full?staticOnly=true`` -> ``model``.
    """
    if not meta or not model:
        return {"loaded": False, "version": (meta or {}).get("version"), "counts": [],
                "carriers": [], "topology": {"buses": [], "branches": [],
                                              "has_coordinates": False, "unattached": 0}}
    sheets = [s for s in meta.get("sheets", []) if s.get("kind") == "static"]
    counts: list[dict[str, Any]] = []
    for s in sheets:
        if s["name"] in _NOT_COMPONENTS or not s.get("rowCount"):
            continue
        counts.append({
            "sheet": s["name"], "label": _label(s["name"]), "count": int(s["rowCount"]),
            "role": sheet_role(s["name"], s.get("columns", [])),
            "columns": list(s.get("columns", [])),
        })

    buses: dict[str, dict[str, Any]] = {}
    for row in model.get("buses", []):
        name = str(row.get("name", ""))
        x, y = _num(row.get("x")), _num(row.get("y"))
        buses[name] = {
            "name": name, "x": x, "y": y, "v_nom_kv": _num(row.get("v_nom")),
            "carrier": str(row.get("carrier") or ""), "assets": [], "assets_total": 0,
        }
    # Many models leave every bus at (0, 0) rather than blank; that is "no
    # coordinates", not a one-point map.
    located = {(b["x"], b["y"]) for b in buses.values() if b["x"] is not None and b["y"] is not None}
    has_coordinates = len(located) > 1

    carrier_assets: dict[str, int] = {}
    unattached = 0
    branches: list[dict[str, Any]] = []
    for c in counts:
        rows = model.get(c["sheet"], [])
        if c["role"] == "attached":
            for row in rows:
                carrier = str(row.get("carrier") or "")
                if carrier:
                    carrier_assets[carrier] = carrier_assets.get(carrier, 0) + 1
                bus = buses.get(str(row.get("bus", "")))
                if bus is None:
                    unattached += 1
                    continue
                bus["assets_total"] += 1
                if len(bus["assets"]) < _MAX_ASSETS_PER_BUS:
                    bus["assets"].append({
                        "sheet": c["sheet"], "name": str(row.get("name", "")),
                        "carrier": carrier,
                        "p_nom_mw": _num(row.get("p_nom")),
                    })
        elif c["role"] == "branch":
            for row in rows:
                branches.append({
                    "sheet": c["sheet"], "name": str(row.get("name", "")),
                    "bus0": str(row.get("bus0", "")), "bus1": str(row.get("bus1", "")),
                    "capacity_mw": _num(row.get("s_nom", row.get("p_nom"))),
                    "carrier": str(row.get("carrier") or ""),
                })

    carriers = [{
        "name": str(r.get("name", "")),
        "nice_name": str(r.get("nice_name") or ""),
        "color": str(r.get("color") or ""),
        "co2_emissions_t_per_mwh_th": _num(r.get("co2_emissions")),
        "asset_count": carrier_assets.get(str(r.get("name", "")), 0),
    } for r in model.get("carriers", [])]

    series = [s for s in meta.get("sheets", []) if s.get("kind") == "series"]
    return {
        "loaded": True,
        "version": meta.get("version"),
        "filename": meta.get("filename") or "",
        "snapshot_count": meta.get("snapshotCount"),
        "snapshot_start": meta.get("snapshotStart"),
        "snapshot_end": meta.get("snapshotEnd"),
        "series_sheets": [{"name": s["name"], "rows": s.get("rowCount"),
                           "columns": list(s.get("columns", []))} for s in series],
        "counts": counts,
        "carriers": carriers,
        "topology": {
            "buses": list(buses.values()), "branches": branches,
            "has_coordinates": has_coordinates, "unattached": unattached,
        },
    }


def table_page(
    sheet: dict[str, Any], *, offset: int, limit: int, q: str | None,
    sort: str | None, descending: bool,
) -> dict[str, Any]:
    """Filter, sort and page one sheet's rows.

    Ragnarok pages but does not filter or sort; doing it here keeps the browser
    holding one page rather than the sheet.
    """
    columns = [str(c) for c in sheet.get("columns", [])]
    rows: list[dict[str, Any]] = list(sheet.get("rows", []))
    # Columns worth showing by default: the key (first) column plus any whose value
    # differs between rows. A 33-column sheet where 25 columns hold the default is
    # mostly noise; the rest are one toggle away.
    def varies(column: str) -> bool:
        seen: set[str] = set()
        for r in rows:
            seen.add(repr(r.get(column)))
            if len(seen) > 1:
                return True
        return False

    key_columns = columns[:1] + [c for c in columns[1:] if varies(c)]
    needle = (q or "").strip().lower()
    if needle:
        rows = [r for r in rows if any(needle in str(v).lower() for v in r.values() if v is not None)]
    if sort and sort in columns:
        present = [r for r in rows if r.get(sort) is not None]
        missing = [r for r in rows if r.get(sort) is None]
        numeric = all(_num(r.get(sort)) is not None for r in present)
        present.sort(
            key=(lambda r: _num(r[sort])) if numeric else (lambda r: str(r[sort]).lower()),  # type: ignore[arg-type,return-value]
            reverse=descending,
        )
        rows = present + missing  # a gap sorts last either way
    total = len(rows)
    return {
        "sheet": sheet.get("name"), "kind": sheet.get("kind"), "columns": columns,
        "key_columns": key_columns,
        "items": rows[offset: offset + limit], "total": total,
        "offset": offset, "limit": limit,
    }


# ── journal, queue, runs ─────────────────────────────────────────────────────


def shape_journal(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    """Newest-first entries; ``next_before`` pages further back, ``None`` at the end."""
    entries = list(payload.get("entries", []))
    return {
        "items": entries, "total": len(entries), "version": payload.get("version"),
        "next_before": entries[-1]["id"] if len(entries) >= limit and entries else None,
    }


def shape_queue(payload: dict[str, Any], now_iso: str | None = None) -> dict[str, Any]:
    """Jobs with ``elapsed_s`` for running ones, so the UI can tick a live timer
    from the server's clock rather than guess at it."""
    from datetime import datetime, timezone

    def parse(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    now = parse(now_iso) if now_iso else datetime.now(timezone.utc)
    items = []
    for job in payload.get("jobs", []):
        started = parse(job.get("startedAt")) if job.get("startedAt") else None
        finished = parse(job.get("finishedAt")) if job.get("finishedAt") else None
        end = finished or now
        elapsed = (end - started).total_seconds() if started and end else None
        items.append({**job, "elapsed_s": round(elapsed, 1) if elapsed is not None else None})
    active = sum(1 for j in items if j.get("status") in ("queued", "running"))
    return {
        "items": items, "total": len(items), "active": active,
        "concurrency": payload.get("concurrency"), "max_concurrency": payload.get("maxConcurrency"),
    }


def shape_runs(payload: dict[str, Any]) -> dict[str, Any]:
    items = [{
        "name": r.get("name"), "label": r.get("label") or "", "saved_at": r.get("savedAt"),
        "origin": r.get("origin") or "", "filename": r.get("filename") or "",
        "snapshot_start": r.get("snapshotStart"), "snapshot_end": r.get("snapshotEnd"),
        "component_counts": r.get("componentCounts") or {},
        "summary": [{"label": k.get("label"), "value": k.get("value")}
                    for k in (r.get("summary") or r.get("kpis") or [])[:4]],
        "size_bytes": r.get("sizeBytes"),
    } for r in payload.get("runs", [])]
    return {"items": items, "total": len(items)}


# ── results dashboard ────────────────────────────────────────────────────────

#: (key, label, unit) for the KPI strip, in reading order. Unknown keys Ragnarok
#: adds later still appear, humanised, after these.
_KPI_LABELS: tuple[tuple[str, str, str], ...] = (
    ("totalCost", "Total cost", ""),
    ("avgPrice", "Average price", "price"),
    ("maxPrice", "Peak price", "price"),
    ("minPrice", "Minimum price", "price"),
    ("peakLoadMw", "Peak load", "MW"),
    ("dispatchMwh", "Energy dispatched", "MWh"),
    ("loadFactor", "Load factor", "%"),
    ("renewableSharePct", "Renewable share", "%"),
    ("emissions", "Emissions", ""),
    ("snapshotCount", "Snapshots", ""),
)
_KPI_HIDDEN = {"currencySymbol", "snapshotWeightHours"}


def shape_kpis(kpis: dict[str, Any]) -> list[dict[str, Any]]:
    """``{key: raw}`` -> ``[{key, label, value, unit}]`` (``value`` numeric when it
    can be, else ``None`` with the Ragnarok text in ``text``)."""
    currency = str(kpis.get("currencySymbol") or "")
    known = {k for k, _, _ in _KPI_LABELS}
    out: list[dict[str, Any]] = []
    ordered = [(k, lab, u) for k, lab, u in _KPI_LABELS if k in kpis]
    ordered += [(k, re.sub(r"(?<!^)(?=[A-Z])", " ", k).capitalize(), "")
                for k in kpis if k not in known and k not in _KPI_HIDDEN]
    for key, label, unit in ordered:
        raw = kpis.get(key)
        value = _num(raw)
        if value is None and key == "totalCost":
            value = _parse_amount(raw)
        shown_unit = f"{currency}/MWh" if unit == "price" else ("" if key == "totalCost" else unit)
        if key == "loadFactor" and value is not None:
            value, shown_unit = value * 100.0, "%"  # Ragnarok sends a fraction
        out.append({
            "key": key, "label": label, "value": value,
            "unit": shown_unit if key != "totalCost" else currency,
            "text": raw if isinstance(raw, str) else None,
        })
    return out


def _rows_by_timestamp(rows: list[dict[str, Any]] | None, keep: set[str]) -> list[dict[str, Any]]:
    return [r for r in (rows or []) if _ts(r.get("timestamp") or r.get("label")) in keep]


def build_dashboard(
    *,
    name: str,
    analytics: dict[str, Any],
    kpis: dict[str, Any],
    generators: list[dict[str, Any]],
    storage_units: list[dict[str, Any]],
    generator_p: dict[str, Any] | None,
    bus: str | None,
    carrier: str | None,
    period: str | None,
) -> dict[str, Any]:
    """Columnar dashboard data for one run, narrowed by bus / carrier / period.

    ``generator_p`` is the run's ``generators-p`` window (needed only when a bus is
    chosen: Ragnarok's by-carrier series is system-wide, so a bus view re-sums the
    per-asset output over the generators at that bus).
    """
    result = analytics.get("result") or {}
    dispatch_rows = list(result.get("dispatchSeries") or [])
    meta = result.get("runMeta") or {}
    snapshot_count = int(meta.get("snapshotCount") or len(dispatch_rows) or 0)
    stride = max(1, round(snapshot_count / len(dispatch_rows))) if dispatch_rows else 1

    periods = sorted({str(r["period"]) for r in dispatch_rows if r.get("period") is not None})
    chosen = [
        (i, r) for i, r in enumerate(dispatch_rows)
        if period is None or str(r.get("period")) == period
    ]
    keep = {_ts(r.get("timestamp")) for _, r in chosen}
    x = [_ts(r.get("timestamp")) for _, r in chosen]
    snapshot_index = [i * stride for i, _ in chosen]

    colours: dict[str, str] = {}
    for src in (result.get("carrierMix") or [], result.get("generatorEnergy") or []):
        for item in src:
            key = item.get("label") or item.get("carrier")
            if key and item.get("color"):
                colours.setdefault(str(key), str(item["color"]))
    gen_meta = {str(g.get("name")): g for g in generators}
    sto_meta = {str(s.get("name")): s for s in storage_units}
    buses = sorted({str(g.get("bus")) for g in generators if g.get("bus")}
                   | {str(s.get("bus")) for s in storage_units if s.get("bus")})
    price_rows = _rows_by_timestamp(result.get("nodalPriceSeries"), keep)
    for r in price_rows[:1]:
        buses = sorted(set(buses) | set((r.get("values") or {}).keys()))

    # The selectors list every carrier the run has, not only those left after the
    # current filter — otherwise choosing a bus would shrink the carrier list under it.
    all_carriers = sorted(
        {str(g.get("carrier")) for g in generators if g.get("carrier")}
        | {str(s.get("carrier")) for s in storage_units if s.get("carrier")}
        | {str(m.get("label")) for m in (result.get("carrierMix") or []) if m.get("label")}
    )

    # Dispatch by carrier ----------------------------------------------------
    dispatch: dict[str, list[float]] = {}
    if bus and generator_p is not None:
        idx = {str(c): j for j, c in enumerate(generator_p.get("columns", []))}
        wanted = [(n, str(g.get("carrier") or "other")) for n, g in gen_meta.items()
                  if str(g.get("bus")) == bus and n in idx]
        prows = {_ts(r.get(generator_p.get("indexCol", "snapshot"))): r
                 for r in generator_p.get("rows", [])}
        for n, carr in wanted:
            col = dispatch.setdefault(carr, [0.0] * len(x))
            for k, t in enumerate(x):
                v = _num((prows.get(t) or {}).get(n))
                if v is not None:
                    col[k] += v
    else:
        for _, r in chosen:
            for carr in (r.get("values") or {}):
                dispatch.setdefault(str(carr), [0.0] * len(x))
        for k, (_, r) in enumerate(chosen):
            for carr, v in (r.get("values") or {}).items():
                dispatch[str(carr)][k] = _num(v) or 0.0
    discharge_rows = _rows_by_timestamp(result.get("storageSeries"), keep)
    if not bus and discharge_rows and any((_num(r.get("discharge")) or 0) > 0 for r in discharge_rows):
        dispatch["storage discharge"] = [_num(r.get("discharge")) or 0.0 for r in discharge_rows]
    if carrier:
        dispatch = {k: v for k, v in dispatch.items() if k == carrier}
    dispatch_series = [
        {"name": k, "values": v, **({"color": colours[k]} if k in colours else {})}
        for k, v in sorted(dispatch.items())
    ]
    load = None
    if not bus and not carrier:
        load = [_num(r.get("total")) for _, r in chosen]
        if all(v is None for v in load):
            load = None

    # Prices -----------------------------------------------------------------
    price_series: list[dict[str, Any]] = []
    if bus:
        price_series.append({"name": bus, "values": [_num((r.get("values") or {}).get(bus)) for r in price_rows]})
    else:
        system = _rows_by_timestamp(result.get("systemPriceSeries"), keep)
        if system:
            price_series.append({"name": "system (load-weighted)", "values": [_num(r.get("value")) for r in system]})
        if price_rows and len(buses) <= _MAX_PRICE_BUS_LINES:
            for b in sorted((price_rows[0].get("values") or {}).keys()):
                price_series.append({"name": b, "values": [_num((r.get("values") or {}).get(b)) for r in price_rows]})

    # Curtailment, emissions, storage -----------------------------------------
    curtail_rows = _rows_by_timestamp(result.get("curtailmentSeries"), keep)
    curtail: dict[str, list[float]] = {}
    for r in curtail_rows:
        for c in (r.get("values") or {}):
            curtail.setdefault(str(c), [0.0] * len(curtail_rows))
    for k, r in enumerate(curtail_rows):
        for c, v in (r.get("values") or {}).items():
            curtail[str(c)][k] = _num(v) or 0.0
    if carrier:
        curtail = {k: v for k, v in curtail.items() if k == carrier}
    curtail_series = [
        {"name": k, "values": v, **({"color": colours[k]} if k in colours else {})}
        for k, v in sorted(curtail.items()) if any(v)
    ]
    emission_rows = _rows_by_timestamp(result.get("systemEmissionsSeries"), keep)
    soc_rows = _rows_by_timestamp(result.get("storageSocSeries"), keep)
    soc: dict[str, list[float | None]] = {}
    for r in soc_rows:
        for u in (r.get("values") or {}):
            if bus and str((sto_meta.get(str(u)) or {}).get("bus")) != bus:
                continue
            soc.setdefault(str(u), [])
    for u in soc:
        soc[u] = [_num((r.get("values") or {}).get(u)) for r in soc_rows]
    storage_flow = {
        "charge": [_num(r.get("charge")) for r in discharge_rows],
        "discharge": [_num(r.get("discharge")) for r in discharge_rows],
    } if discharge_rows and not bus else None

    # Capacity: installed vs optimised, by carrier ---------------------------------
    static_out = ((result.get("outputs") or {}).get("static") or {})
    cap: dict[str, dict[str, float]] = {}
    for sheet, rows in (("generators", generators), ("storage_units", storage_units)):
        opt = static_out.get(sheet) or {}
        for row in rows:
            if bus and str(row.get("bus")) != bus:
                continue
            carr = str(row.get("carrier") or sheet)
            if carrier and carr != carrier:
                continue
            installed = _num(row.get("p_nom")) or 0.0
            optimal = _num((opt.get(str(row.get("name"))) or {}).get("p_nom_opt"))
            slot = cap.setdefault(carr, {"installed": 0.0, "optimal": 0.0})
            slot["installed"] += installed
            slot["optimal"] += installed if optimal is None else optimal
    categories = sorted(cap)
    capacity = {
        "categories": categories,
        "series": [
            {"name": "installed", "values": [cap[c]["installed"] for c in categories]},
            {"name": "optimal", "values": [cap[c]["optimal"] for c in categories]},
        ],
    }
    expansion = [e for e in (result.get("expansionResults") or [])
                 if (not bus or str(e.get("bus")) == bus) and (not carrier or str(e.get("carrier")) == carrier)]

    # Emissions by carrier ---------------------------------------------------------
    em_by_carrier: dict[str, dict[str, float]] = {}
    for g in ((result.get("emissionsBreakdown") or {}).get("byGenerator") or []):
        if bus and str(g.get("bus")) != bus:
            continue
        carr = str(g.get("carrier") or "other")
        if carrier and carr != carrier:
            continue
        slot = em_by_carrier.setdefault(carr, {"energy_mwh": 0.0, "emissions_tco2": 0.0})
        slot["energy_mwh"] += _num(g.get("energy_mwh")) or 0.0
        slot["emissions_tco2"] += _num(g.get("emissions_tco2")) or 0.0
    em_cats = sorted(em_by_carrier)

    cost = [{"label": c.get("label"), "value": _num(c.get("value"))}
            for c in (result.get("costBreakdown") or []) if (_num(c.get("value")) or 0) != 0]
    line_loading = [{"name": str(c.get("label")), "loading_pct": _num(c.get("value"))}
                    for c in (result.get("lineLoading") or [])]
    mix = [{"name": str(m.get("label")), "energy_mwh": _num(m.get("value")),
            **({"color": m["color"]} if m.get("color") else {})}
           for m in (result.get("carrierMix") or []) if not carrier or m.get("label") == carrier]

    return {
        "run": name, "label": analytics.get("label") or "", "saved_at": analytics.get("savedAt"),
        "origin": analytics.get("origin") or "",
        "kpis": shape_kpis(kpis), "summary": result.get("summary") or [],
        "snapshot_count": snapshot_count, "stride": stride, "periods": periods,
        "buses": buses, "carriers": all_carriers,
        "selected": {"bus": bus, "carrier": carrier, "period": period},
        "x": x, "snapshot_index": snapshot_index,
        "dispatch_mw": {"series": dispatch_series, "load_mw": load},
        "price": {"series": price_series, "currency": str(kpis.get("currencySymbol") or "")},
        "curtailment_mw": {"series": curtail_series},
        "emissions_tco2e": {"values": [_num(r.get("value")) for r in emission_rows]},
        "storage": {
            "soc_mwh": [{"name": u, "values": v} for u, v in sorted(soc.items())],
            "flow_mw": storage_flow,
        },
        "capacity_mw": capacity,
        "expansion": expansion,
        "emissions_by_carrier": {
            "categories": em_cats,
            "series": [
                {"name": "emissions_tco2", "values": [em_by_carrier[c]["emissions_tco2"] for c in em_cats]},
                {"name": "energy_mwh", "values": [em_by_carrier[c]["energy_mwh"] for c in em_cats]},
            ],
        },
        "cost_breakdown": cost, "carrier_mix": mix, "line_loading": line_loading,
        "bus_filter_note": (
            "Dispatch, price, storage and capacity are for this bus; curtailment and emissions "
            "over time are system-wide." if bus else None
        ),
    }


def build_snapshot(
    *, index: int, label: str, generator_p: dict[str, Any], load_p: dict[str, Any] | None,
    price: dict[str, Any] | None, flows: dict[str, Any] | None,
    generators: list[dict[str, Any]], colours: dict[str, str], bus: str | None,
) -> dict[str, Any]:
    """One snapshot's dispatch by carrier, asset and bus: what a click on a chart's
    time point opens. Inputs are one-row windows of the run's output sheets."""
    gen_meta = {str(g.get("name")): g for g in generators}

    def first(window: dict[str, Any] | None) -> dict[str, Any]:
        rows = (window or {}).get("rows") or []
        return rows[0] if rows else {}

    p_row = first(generator_p)
    by_asset = []
    for asset, value in p_row.items():
        if asset == (generator_p or {}).get("indexCol", "snapshot"):
            continue
        g = gen_meta.get(str(asset), {})
        if bus and str(g.get("bus")) != bus:
            continue
        v = _num(value)
        if v is None:
            continue
        by_asset.append({"name": str(asset), "carrier": str(g.get("carrier") or ""),
                         "bus": str(g.get("bus") or ""), "p_mw": v})
    by_asset.sort(key=lambda a: -abs(a["p_mw"]))
    carriers: dict[str, float] = {}
    for a in by_asset:
        carriers[a["carrier"] or "other"] = carriers.get(a["carrier"] or "other", 0.0) + a["p_mw"]
    by_carrier = [{"name": k, "p_mw": v, **({"color": colours[k]} if k in colours else {})}
                  for k, v in sorted(carriers.items(), key=lambda kv: -abs(kv[1]))]

    price_row, load_row = first(price), first(load_p)
    by_bus: dict[str, dict[str, Any]] = {}
    for b, v in price_row.items():
        if b != (price or {}).get("indexCol", "snapshot"):
            by_bus.setdefault(b, {"bus": b})["price"] = _num(v)
    for a in by_asset:
        by_bus.setdefault(a["bus"], {"bus": a["bus"]})
        by_bus[a["bus"]]["generation_mw"] = (by_bus[a["bus"]].get("generation_mw") or 0.0) + a["p_mw"]
    flow_row = first(flows)
    return {
        "index": index, "label": label, "bus": bus,
        "by_carrier": by_carrier, "by_asset": by_asset[:50],
        "by_bus": [{"generation_mw": None, "price": None, **v} for v in by_bus.values()
                   if not bus or v.get("bus") == bus],
        "load_mw": sum(v for k, v in load_row.items()
                       if k != (load_p or {}).get("indexCol", "snapshot") and _num(v) is not None) if load_row else None,
        "flows": [{"name": k, "p0_mw": _num(v)} for k, v in flow_row.items()
                  if k != (flows or {}).get("indexCol", "snapshot") and _num(v) is not None],
    }


def build_comparison(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Two to five runs side by side.

    Each entry: ``{"name", "analytics", "kpis"}``. The first run is the baseline;
    ``delta`` and ``delta_pct`` are every other run minus it.
    """
    runs = [{"name": e["name"], "label": (e["analytics"].get("label") or ""),
             "saved_at": e["analytics"].get("savedAt")} for e in entries]
    shaped = [{k["key"]: k for k in shape_kpis(e["kpis"])} for e in entries]
    keys: list[str] = []
    for s in shaped:
        for k in s:
            if k not in keys:
                keys.append(k)
    rows = []
    for k in keys:
        cells = [s.get(k) for s in shaped]
        values = [(c or {}).get("value") for c in cells]
        base = values[0]
        meta = next(c for c in cells if c)
        rows.append({
            "key": k, "label": meta["label"], "unit": meta["unit"], "values": values,
            "text": [(c or {}).get("text") for c in cells],
            "delta": [None if (v is None or base is None) else v - base for v in values],
            "delta_pct": [None if (v is None or base in (None, 0)) else (v - base) / abs(base) * 100.0
                          for v in values],
        })

    def by_label(entry: dict[str, Any], key: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for item in (entry["analytics"].get("result") or {}).get(key) or []:
            v = _num(item.get("value"))
            if v is not None:
                out[str(item.get("label"))] = v
        return out

    def category_chart(per_run: list[dict[str, float]]) -> dict[str, Any]:
        cats = sorted({c for d in per_run for c in d})
        return {"categories": cats, "series": [
            {"name": r["name"], "values": [d.get(c, 0.0) for c in cats]} for r, d in zip(runs, per_run)]}

    def capacity(entry: dict[str, Any]) -> dict[str, float]:
        out: dict[str, float] = {}
        stat = ((entry["analytics"].get("result") or {}).get("statistics") or {}).get("rows") or []
        for row in stat:
            v = _num((row.get("values") or {}).get("Optimal Capacity"))
            if v is not None and row.get("component") in ("Generator", "StorageUnit", "Store"):
                out[str(row.get("carrier"))] = out.get(str(row.get("carrier")), 0.0) + v
        return out

    price: list[dict[str, Any]] = []
    for r, e in zip(runs, entries):
        rows_ = (e["analytics"].get("result") or {}).get("systemPriceSeries") or []
        price.append({"name": r["name"], "x": [_ts(p.get("timestamp")) for p in rows_],
                      "values": [_num(p.get("value")) for p in rows_]})
    return {
        "runs": runs, "kpis": rows,
        "generation_mwh": category_chart([by_label(e, "carrierMix") for e in entries]),
        "capacity_mw": category_chart([capacity(e) for e in entries]),
        "cost": category_chart([by_label(e, "costBreakdown") for e in entries]),
        "price": price,
    }
