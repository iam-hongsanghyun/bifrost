"""Turn the agent's ``show_chart`` arguments into a ``chart.show`` payload.

This used to live inline in the agent service and did three quiet harms:

* it cut ``x`` to the first 2,000 points, so a year of hourly snapshots (8,760)
  drew January to March and still reported "Drawn";
* it dropped every non-numeric value from a series BEFORE aligning it to ``x``, so
  one missing hour shifted the rest of the series a step left against its labels;
* it never said it had done either.

Here a value that is not a finite number becomes ``None`` (a gap) and keeps its
position; length is padded or cut to ``x`` rather than silently shifting; and when a
chart really is too big for the event stream it is thinned with ONE shared set of
indices, keeping each bucket's peak and trough, so every series still lines up with
its labels and the scarcity hour is still on the chart. The payload says how many
points the agent supplied so the UI can show "N of M".

Pure functions, no I/O: tested directly.
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any


#: A colour the canvas will take: hex, or a bare CSS colour name. Anything else
#: (url(), var(), expressions) is ignored rather than passed through.
_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}")


def _number(value: Any) -> float | None:
    """A finite float, or ``None``. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def thin_indices(columns: list[list[float | None]], target: int) -> list[int]:
    """Indices to keep so every column can be cut to ``target`` points together.

    Per bucket of ``ceil(n / (target / 2))`` rows, keep the row where the column
    SUM is largest and the row where it is smallest (plus the very first and last
    rows). The same indices apply to every column, so stacks still add up and a
    series stays aligned with the x labels.

    Args:
        columns: One list of values per series, all the same length ``n``.
        target: Approximate number of rows wanted (at least 4).

    Returns:
        Sorted, unique row indices into the original ``n`` rows.
    """
    n = len(columns[0]) if columns else 0
    target = max(4, target)
    if n <= target:
        return list(range(n))
    totals = [
        sum(col[i] for col in columns if col[i] is not None) for i in range(n)
    ]
    bucket = math.ceil(n / (target / 2))
    keep = {0, n - 1}
    for start in range(0, n, bucket):
        window = range(start, min(start + bucket, n))
        keep.add(max(window, key=totals.__getitem__))
        keep.add(min(window, key=totals.__getitem__))
    return sorted(keep)


def build_chart_payload(
    args: dict[str, Any],
    *,
    conversation_id: str | None,
    max_points: int,
    max_cells: int,
) -> dict[str, Any] | str:
    """The ``chart.show`` payload for ``args``, or a sentence saying why not.

    Args:
        args: What the agent passed to ``show_chart``.
        conversation_id: Stamped on the payload so the transcript can file it.
        max_points: Most x points in the event (``BIFROST_CHART_MAX_POINTS``).
        max_cells: Most points x series in the event (``BIFROST_CHART_MAX_CELLS``).
    """
    kind = str(args.get("kind") or "line")
    x_all = [str(v) for v in (args.get("x") or [])]
    if not x_all:
        return "Not drawn: a chart needs an x axis and at least one series with numbers."
    n = len(x_all)

    series: list[dict[str, Any]] = []
    for raw in args.get("series") or []:
        if not isinstance(raw, dict):
            continue
        values: list[float | None] = [_number(v) for v in (raw.get("values") or [])][:n]
        values += [None] * (n - len(values))  # pad: never shift
        if all(v is None for v in values):
            continue
        entry: dict[str, Any] = {"name": str(raw.get("name") or "series"), "values": values}
        # A price in $/MWh on the same axis as dispatch in MW makes one of them
        # invisible, so a series can opt out onto the right-hand axis — and opt
        # out of the stack, since stacking a price is meaningless.
        if str(raw.get("axis") or "").lower() == "right":
            entry["axis"] = "right"
        if raw.get("kind"):
            entry["kind"] = str(raw["kind"])
        if raw.get("color") and _COLOUR.fullmatch(str(raw["color"])):
            entry["color"] = str(raw["color"])
        series.append(entry)
    if not series:
        return "Not drawn: a chart needs an x axis and at least one series with numbers."

    x = x_all
    thinned_from: int | None = None
    if kind == "heatmap":
        # Rows x columns IS the picture; thinning would change what a cell means.
        if n * len(series) > max_cells:
            return (
                f"Not drawn: a {len(series)} x {n} heatmap is {n * len(series):,} cells; "
                f"the limit is {max_cells:,}. Aggregate it (e.g. daily means) and draw again."
            )
    else:
        budget = max(4, min(max_points, max_cells // max(1, len(series))))
        if n > budget:
            keep = thin_indices([s["values"] for s in series], budget)
            x = [x_all[i] for i in keep]
            for s in series:
                s["values"] = [s["values"][i] for i in keep]
            thinned_from = n

    payload: dict[str, Any] = {
        "conversationId": conversation_id,
        "chartId": uuid.uuid4().hex[:12],
        "title": str(args.get("title") or ""),
        # `chartKind`, not `kind`: the event envelope already uses `kind` for
        # the event name, and overloading it made the chart type unreadable.
        "chartKind": kind,
        "unit": str(args.get("unit") or ""),
        "unitRight": str(args.get("unitRight") or ""),
        "stacked": bool(args.get("stacked")),
        "note": str(args.get("note") or ""),
        "x": x,
        "series": series,
    }
    if thinned_from is not None:
        payload["thinnedFrom"] = thinned_from
    return payload
