"""Validate rows a user supplies against Ragnarok's live schema (pure).

The data-ask loop: when a gap cannot be filled from open data (a private fleet,
a confidential topology) the agent calls ``request_data``; the user pastes CSV
or drops a file; THIS module decides whether those rows may enter the model.
Nothing here does I/O — the schema, the existing buses and carriers and the text
are arguments — so every rule is unit-testable without an engine.

What is checked, and why it is checked here rather than left to the merge:

* **Columns.** An unknown column is an ERROR, not an ignored extra: ``p_nomm``
  dropped silently is a fleet with no capacity that still "merges cleanly".
* **Required values.** Ragnarok's schema marks ``name`` and ``bus`` required; the
  agent may also name the columns *it* needs, and those are required for this ask.
* **Types.** float / int / boolean per the schema; NaN and inf are refused.
* **Units.** A header may say ``p_nom [kW]``; a known conversion to the schema's
  unit is applied, an unknown mismatch is refused rather than guessed.
* **References.** ``bus`` / ``bus0`` / ``bus1``… must name a bus in the model.
  ``carrier`` not yet in the carriers sheet is a WARNING: the merge adds it.
* **Names.** Duplicates inside the paste are errors; a clash with an existing
  component is a warning, because Ragnarok's merge renames it ``name_2``.

Errors carry the 1-based LINE of the pasted text (the header is line 1) so the
user can find the row they typed.
"""
from __future__ import annotations

import csv
import difflib
import io
import math
import re
from dataclasses import dataclass, field
from typing import Any

#: Schema ``type`` strings → the four kinds this module validates. Anything not
#: here (``series``, ``pandas.DataFrame``…) is not a static input column.
_TYPE_KINDS = {
    "float": "float", "static": "float", "static or series": "float",
    "static or piecewise or series": "float",
    "int": "int", "integer": "int",
    "boolean": "boolean",
    "string": "string",
}

BUS_REFS = ("bus", "bus0", "bus1", "bus2", "bus3", "bus4")
CARRIER_REF = "carrier"
#: Sheets that are not a table of components a person can hand over.
NOT_ASKABLE = {"snapshots", "network", "sub_networks", "shapes"}

_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}

# unit → factor to the family's base (MW, MWh, kV, per unit).
_FAMILIES: dict[str, dict[str, float]] = {
    "power": {"w": 1e-6, "kw": 1e-3, "mw": 1.0, "gw": 1e3, "tw": 1e6},
    "energy": {"wh": 1e-6, "kwh": 1e-3, "mwh": 1.0, "gwh": 1e3, "twh": 1e6},
    "voltage": {"v": 1e-3, "kv": 1.0, "mv": 1e3},
    "fraction": {"pu": 1.0, "per unit": 1.0, "p.u.": 1.0, "%": 0.01},
}
_UNIT_HEADER = re.compile(r"^(?P<name>.*?)\s*[\[(](?P<unit>[^\])]+)[\])]\s*$")


class DataAskError(ValueError):
    """The request itself is unusable (unknown component / column)."""


@dataclass(frozen=True)
class Column:
    name: str
    kind: str  # float | int | boolean | string
    unit: str
    required: bool
    description: str
    default: str

    def card(self, needed: bool) -> dict[str, Any]:
        return {
            "name": self.name, "type": self.kind, "unit": self.unit,
            "required": needed, "description": self.description, "default": self.default,
        }


@dataclass(frozen=True)
class SheetSpec:
    sheet: str
    component: str
    label: str
    columns: tuple[Column, ...]
    #: Columns a row MUST carry for this ask: the schema's required ones plus
    #: whatever the agent said it needs.
    needed: tuple[str, ...]

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


def spec_from_config(config: dict[str, Any], component: str, wanted: list[str] | None = None) -> SheetSpec:
    """Build the sheet spec from Ragnarok's ``/api/config`` bundle.

    Raises :class:`DataAskError` with the valid names when the component or a
    wanted column is unknown, so the agent can correct itself in one step.
    """
    comps = (config.get("schema") or {}).get("components") or {}
    low = component.strip().lower()
    sheet = None
    for key, spec in comps.items():
        names = {key.lower(), str(spec.get("component_name", "")).lower(), str(spec.get("list_name", "")).lower()}
        if low in names:
            sheet = key
            break
    if sheet is None or sheet in NOT_ASKABLE:
        askable = sorted(k for k in comps if k not in NOT_ASKABLE)
        raise DataAskError(f"unknown or non-tabular component {component!r}; valid sheets: {', '.join(askable)}")
    spec = comps[sheet]
    static_inputs = set(spec.get("input_static_attributes") or spec.get("static_attributes") or [])
    columns: list[Column] = []
    for attr in spec.get("attributes") or []:
        name = attr.get("attribute")
        kind = _TYPE_KINDS.get(str(attr.get("type", "")).strip().lower())
        if not name or kind is None or attr.get("status") != "input":
            continue
        if static_inputs and name not in static_inputs:
            continue
        columns.append(Column(
            name=name, kind=kind, unit=str(attr.get("unit") or "n/a"),
            required=bool(attr.get("required")), description=str(attr.get("description") or "").strip(),
            default=str(attr.get("default") or ""),
        ))
    by_name = {c.name: c for c in columns}
    needed = [c.name for c in columns if c.required]
    for raw in wanted or []:
        name = str(raw).strip()
        if name not in by_name:
            close = difflib.get_close_matches(name, by_name, n=3)
            hint = f" (did you mean {', '.join(close)}?)" if close else ""
            raise DataAskError(
                f"{sheet} has no input column {name!r}{hint}; valid columns: {', '.join(by_name)}"
            )
        if name not in needed:
            needed.append(name)
    return SheetSpec(
        sheet=sheet, component=str(spec.get("component_name") or sheet),
        label=str(spec.get("label") or sheet), columns=tuple(columns), needed=tuple(needed),
    )


# ── parsing ─────────────────────────────────────────────────────────────────────

def parse_table(text: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """Split pasted text into ``(header, [(line_number, cells)])``.

    The delimiter is detected from the header line (tab from a spreadsheet paste,
    ``;`` from a European export, else comma). Blank lines are skipped.
    """
    text = text.lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], []
    head = lines[0]
    delimiter = max((",", "\t", ";"), key=lambda d: head.count(d))
    if head.count(delimiter) == 0:
        delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True)
    header: list[str] = []
    rows: list[tuple[int, list[str]]] = []
    for cells in reader:
        if not any(c.strip() for c in cells):
            continue
        if not header:
            header = [c.strip() for c in cells]
            continue
        rows.append((reader.line_num, [c.strip() for c in cells]))
    return header, rows


def _unit_factor(given: str, target: str) -> float | None:
    """Multiplier turning ``given`` into ``target``; ``None`` when unknown."""
    g, t = given.strip().lower(), target.strip().lower()
    if g == t:
        return 1.0
    for family in _FAMILIES.values():
        if g in family and t in family:
            return family[g] / family[t]
    return None


def _parse_value(raw: str, kind: str) -> Any:
    if kind == "string":
        return raw
    if kind == "boolean":
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"{raw!r} is not a boolean (use true/false)")
    try:
        number = float(raw.replace("_", ""))
    except ValueError:
        raise ValueError(f"{raw!r} is not a number") from None
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"{raw!r} is not a finite number")
    if kind == "int":
        if number != int(number):
            raise ValueError(f"{raw!r} is not a whole number")
        return int(number)
    return number


def _predict_rename(name: str, taken: set[str]) -> str:
    """Mirror of Ragnarok's merge dedupe, so the warning names the real result."""
    i = 2
    while f"{name}_{i}" in taken:
        i += 1
    return f"{name}_{i}"


# ── validation ──────────────────────────────────────────────────────────────────

@dataclass
class Report:
    sheet: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    new_carriers: list[str] = field(default_factory=list)
    renames: dict[str, str] = field(default_factory=dict)
    row_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "sheet": self.sheet, "rowCount": self.row_count,
            "columns": self.columns, "errors": self.errors, "warnings": self.warnings,
            "newCarriers": self.new_carriers, "renames": self.renames,
            "preview": self.rows[:5],
        }


def _issue(line: int | None, column: str | None, message: str) -> dict[str, Any]:
    return {"line": line, "column": column, "message": message}


def validate(
    spec: SheetSpec,
    text: str,
    *,
    buses: set[str],
    carriers: set[str],
    existing_names: set[str],
    max_rows: int = 5_000,
) -> Report:
    """Validate pasted CSV text. Never raises on bad DATA — errors are the product."""
    report = Report(sheet=spec.sheet)
    header_raw, data = parse_table(text)
    if not header_raw:
        report.errors.append(_issue(None, None, "Nothing to validate: paste a header line and at least one row."))
        return report

    # Header → schema columns (with optional [unit] suffix and a unit conversion).
    mapping: list[tuple[Column, float] | None] = []
    seen: set[str] = set()
    for raw in header_raw:
        label, unit = raw, ""
        m = _UNIT_HEADER.match(raw)
        if m:
            label, unit = m.group("name").strip(), m.group("unit").strip()
        key = re.sub(r"\s+", "_", label.strip()).lower()
        column = spec.column(key)
        if column is None:
            close = difflib.get_close_matches(key, [c.name for c in spec.columns], n=3)
            hint = f" Did you mean {', '.join(close)}?" if close else ""
            report.errors.append(_issue(1, raw, f"Unknown column {raw!r} for {spec.sheet}.{hint}"))
            mapping.append(None)
            continue
        if column.name in seen:
            report.errors.append(_issue(1, raw, f"Column {column.name!r} appears twice."))
            mapping.append(None)
            continue
        seen.add(column.name)
        factor = 1.0
        if unit:
            f = _unit_factor(unit, column.unit)
            if f is None:
                report.errors.append(_issue(
                    1, raw, f"{column.name} is in {column.unit}; {unit!r} cannot be converted — "
                            f"give the values in {column.unit}.",
                ))
                mapping.append(None)
                continue
            factor = f
        mapping.append((column, factor))

    for need in spec.needed:
        if need not in seen:
            report.errors.append(_issue(1, need, f"Required column {need!r} is missing."))
    report.columns = [m[0].name for m in mapping if m]

    if not data:
        report.errors.append(_issue(None, None, "The header has no data rows under it."))
        return report
    if len(data) > max_rows:
        report.errors.append(_issue(None, None, f"{len(data)} rows is over the {max_rows}-row limit for a paste; import a file instead."))
        return report
    report.row_count = len(data)

    names_seen: dict[str, int] = {}
    taken = set(existing_names)
    carriers_new: list[str] = []
    for line, cells in data:
        if len(cells) > len(header_raw):
            report.errors.append(_issue(line, None, f"{len(cells)} values but the header has {len(header_raw)} columns."))
            continue
        cells = cells + [""] * (len(header_raw) - len(cells))
        row: dict[str, Any] = {}
        row_ok = True
        for cell, mapped in zip(cells, mapping):
            if mapped is None:
                continue
            column, factor = mapped
            if cell == "":
                continue
            try:
                value = _parse_value(cell, column.kind)
            except ValueError as exc:
                report.errors.append(_issue(line, column.name, f"{column.name}: {exc}."))
                row_ok = False
                continue
            if factor != 1.0 and isinstance(value, (int, float)) and not isinstance(value, bool):
                value = value * factor
            row[column.name] = value
        for need in spec.needed:
            if need in seen and need not in row and row_ok:
                report.errors.append(_issue(line, need, f"{need} is required but empty."))
                row_ok = False
        for ref in BUS_REFS:
            value = row.get(ref)
            if isinstance(value, str) and value not in buses:
                close = difflib.get_close_matches(value, sorted(buses), n=2)
                known = ", ".join(sorted(buses)[:8]) or "(the model has no buses yet)"
                hint = f" Did you mean {' or '.join(close)}?" if close else f" Buses in the model: {known}."
                report.errors.append(_issue(line, ref, f"{ref} {value!r} is not a bus in the model.{hint}"))
                row_ok = False
        carrier = row.get(CARRIER_REF)
        if isinstance(carrier, str) and spec.sheet != "carriers" and carrier not in carriers and carrier not in carriers_new:
            carriers_new.append(carrier)
            report.warnings.append(_issue(line, CARRIER_REF, f"carrier {carrier!r} is not in the model yet; it will be added to carriers."))
        name = row.get("name")
        if isinstance(name, str):
            if name in names_seen:
                report.errors.append(_issue(line, "name", f"name {name!r} repeats line {names_seen[name]}."))
                row_ok = False
            else:
                names_seen[name] = line
                if name in existing_names:
                    new = _predict_rename(name, taken)
                    taken.add(new)
                    report.renames[name] = new
                    report.warnings.append(_issue(line, "name", f"{name!r} already exists; it will be saved as {new!r}."))
                else:
                    taken.add(name)
        if row_ok:
            report.rows.append(row)
    report.new_carriers = carriers_new
    return report


def rows_to_fragment(spec: SheetSpec, report: Report, *, stamp: str) -> dict[str, Any]:
    """The ``WorkbookFragment`` Ragnarok's merge takes: validated rows (+ any new
    carriers, bare) and a provenance row naming this as user-supplied."""
    sheets: dict[str, list[dict[str, Any]]] = {spec.sheet: report.rows}
    if report.new_carriers:
        sheets["carriers"] = [{"name": c} for c in report.new_carriers]
    return {
        "sheets": sheets,
        "provenance": {
            "database_id": "user_supplied",
            "fetch_timestamp": stamp,
            "filters_json": '{"via":"bifrost request_data"}',
            "row_counts_json": '{"%s": %d}' % (spec.sheet, len(report.rows)),
        },
    }
