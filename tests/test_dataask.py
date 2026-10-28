"""Validation of user-supplied rows against Ragnarok's schema (pure, no engine)."""
from __future__ import annotations

import pytest

from bifrost import dataask


def _attr(name, type_="float", unit="n/a", required=False, default=""):
    return {"attribute": name, "type": type_, "unit": unit, "required": required,
            "status": "input", "default": default, "description": f"the {name}"}


CONFIG = {"schema": {"components": {
    "generators": {
        "component_name": "Generator", "list_name": "generators", "label": "Generator",
        "input_static_attributes": ["name", "bus", "carrier", "p_nom", "p_nom_extendable", "build_year", "marginal_cost", "p_max_pu"],
        "attributes": [
            _attr("name", "string", required=True), _attr("bus", "string", required=True),
            _attr("carrier", "string"), _attr("p_nom", "float", "MW"),
            _attr("p_nom_extendable", "boolean"), _attr("build_year", "int", "year"),
            _attr("marginal_cost", "static or piecewise or series", "currency/MWh"),
            _attr("p_max_pu", "static or series", "per unit"),
            {**_attr("p", "series", "MW"), "status": "output"},
        ],
    },
    "snapshots": {"component_name": "Snapshot", "attributes": []},
}}}
BUSES = {"north", "south"}


def spec(wanted=None):
    return dataask.spec_from_config(CONFIG, "Generator", wanted)


def check(text, *, existing=frozenset(), carriers=frozenset({"gas"}), wanted=("carrier", "p_nom")):
    return dataask.validate(spec(list(wanted)), text, buses=BUSES, carriers=set(carriers), existing_names=set(existing))


def test_a_clean_paste_validates_and_coerces_types() -> None:
    r = check("name,bus,carrier,p_nom,p_nom_extendable,build_year\nfleet1,north,gas,120.5,yes,2031\n")
    assert r.ok and r.errors == [] and r.warnings == []
    assert r.rows == [{"name": "fleet1", "bus": "north", "carrier": "gas", "p_nom": 120.5,
                       "p_nom_extendable": True, "build_year": 2031}]


def test_spec_names_required_and_requested_columns_and_skips_outputs() -> None:
    s = spec(["p_nom", "carrier"])
    assert s.needed == ("name", "bus", "p_nom", "carrier")
    assert s.column("p") is None  # an output, not an input a person supplies


def test_unknown_component_and_column_say_what_is_valid() -> None:
    with pytest.raises(dataask.DataAskError, match="valid sheets: generators"):
        dataask.spec_from_config(CONFIG, "snapshots")
    with pytest.raises(dataask.DataAskError, match="did you mean p_nom"):
        spec(["p_nomm"])


def test_missing_required_column_is_a_column_level_error() -> None:
    r = check("name,carrier,p_nom\nx,gas,1\n")
    assert not r.ok
    assert {"line": 1, "column": "bus", "message": "Required column 'bus' is missing."} in r.errors


def test_an_unknown_column_is_an_error_not_silently_dropped() -> None:
    r = check("name,bus,carrier,p_nomm\nx,north,gas,5\n")
    assert any(e["column"] == "p_nomm" and "Did you mean p_nom" in e["message"] for e in r.errors)


def test_row_errors_carry_the_line_of_the_paste() -> None:
    r = check("name,bus,carrier,p_nom\nok,north,gas,10\nbad,north,gas,ten\nalso,north,gas,\n")
    by_line = {(e["line"], e["column"]) for e in r.errors}
    assert (3, "p_nom") in by_line          # not a number
    assert (4, "p_nom") in by_line          # asked for, empty
    assert not any(e["line"] == 2 for e in r.errors)
    assert [row["name"] for row in r.rows] == ["ok"]   # the good row is still parsed
    assert not r.ok                          # but one bad row blocks the merge


def test_nan_inf_and_fractional_ints_are_refused() -> None:
    r = check("name,bus,carrier,p_nom,build_year\na,north,gas,nan,2030\nb,north,gas,inf,2030\nc,north,gas,1,2030.5\n")
    assert len(r.errors) == 3


def test_bus_reference_must_exist_and_suggests_the_close_one() -> None:
    r = check("name,bus,carrier,p_nom\na,nroth,gas,1\n")
    (e,) = r.errors
    assert e["column"] == "bus" and "Did you mean north" in e["message"]


def test_unknown_carrier_is_a_warning_and_is_added_by_the_fragment() -> None:
    r = check("name,bus,carrier,p_nom\na,north,wind,1\nb,south,wind,2\n")
    assert r.ok and r.new_carriers == ["wind"]
    assert len([w for w in r.warnings if w["column"] == "carrier"]) == 1
    fragment = dataask.rows_to_fragment(spec(), r, stamp="2030-01-01T00:00:00+00:00")
    assert fragment["sheets"]["carriers"] == [{"name": "wind"}]
    assert fragment["provenance"]["database_id"] == "user_supplied"


def test_duplicate_names_in_the_paste_are_errors_existing_names_are_renamed_warnings() -> None:
    r = check("name,bus,carrier,p_nom\na,north,gas,1\na,south,gas,2\n", existing={"b"})
    assert any("repeats line 2" in e["message"] for e in r.errors)
    r = check("name,bus,carrier,p_nom\nb,north,gas,1\n", existing={"b", "b_2"})
    assert r.ok and r.renames == {"b": "b_3"}


def test_units_in_the_header_are_converted_or_refused() -> None:
    r = check("name,bus,carrier,p_nom [kW]\na,north,gas,2500\n")
    assert r.ok and r.rows[0]["p_nom"] == pytest.approx(2.5)
    r = check("name,bus,carrier,p_nom (GW)\na,north,gas,1.2\n")
    assert r.rows[0]["p_nom"] == pytest.approx(1200.0)
    r = check("name,bus,carrier,p_nom [MWh]\na,north,gas,2\n")   # energy is not power
    assert not r.ok and "cannot be converted" in r.errors[0]["message"]
    r = check("name,bus,carrier,p_nom,p_max_pu [%]\na,north,gas,1,95\n", wanted=("p_nom",))
    assert r.rows[0]["p_max_pu"] == pytest.approx(0.95)


def test_tab_and_semicolon_pastes_and_blank_lines_and_bom() -> None:
    r = check("﻿name\tbus\tcarrier\tp_nom\n\na\tnorth\tgas\t3\n")
    assert r.ok and r.rows[0]["p_nom"] == 3.0
    r = check("name;bus;carrier;p_nom\na;north;gas;3\n")
    assert r.ok


def test_empty_header_only_and_oversized_pastes() -> None:
    assert not check("").ok
    assert "no data rows" in check("name,bus\n").errors[-1]["message"]
    r = dataask.validate(spec(), "name,bus,carrier,p_nom\n" + "\n".join(f"g{i},north,gas,1" for i in range(6)),
                         buses=BUSES, carriers={"gas"}, existing_names=set(), max_rows=5)
    assert "limit" in r.errors[0]["message"]


def test_ragged_rows_are_reported() -> None:
    r = check("name,bus,carrier,p_nom\na,north,gas,1,extra\n")
    assert "5 values but the header has 4" in r.errors[0]["message"]
