/** Chart specs derived on the client from data the dashboard already holds. */
import { CategoryChart, Dashboard, NamedSeries } from '../api';
import { ChartSpec } from './chartModel';

/** Pad or cut `values` to `n` so a series that is short stays aligned (gaps, never shifts). */
export function fit(values: (number | null)[], n: number): (number | null)[] {
  if (values.length === n) return values;
  const out = values.slice(0, n);
  while (out.length < n) out.push(null);
  return out;
}

export function timeSpec(
  dash: Dashboard, id: string, title: string, unit: string, series: NamedSeries[],
  extra: Partial<ChartSpec> = {},
): ChartSpec {
  return {
    chartId: `${dash.run}:${id}:${dash.selected.bus ?? ''}:${dash.selected.carrier ?? ''}:${dash.selected.period ?? ''}`,
    title, unit, x: dash.x,
    series: series.map((s) => ({ name: s.name, values: fit(s.values, dash.x.length), color: s.color })),
    ...extra,
  };
}

export function categorySpec(
  run: string, id: string, title: string, unit: string, chart: CategoryChart, extra: Partial<ChartSpec> = {},
): ChartSpec {
  return { chartId: `${run}:${id}`, title, unit, kind: 'bar', x: chart.categories, series: chart.series, ...extra };
}

/** Past this many days a row is thinner than a pixel; average whole weeks instead. */
const MAX_DAY_ROWS = 120;
const WEEK = 7;

/**
 * Day x hour heatmap of one series, for the "is there a pattern" question a line
 * flattens away. Needs labels like `2030-06-01 08:00` covering at least two days.
 * A year of days is averaged into weeks (one row per week, named for its first day)
 * rather than drawn as 365 rows thinner than the screen can show.
 */
export function dayHourHeatmap(
  x: string[], values: (number | null)[], id: string, title: string, unit: string,
): ChartSpec | null {
  const days = new Map<string, Map<string, { sum: number; n: number }>>();
  const hours = new Set<string>();
  x.forEach((label, i) => {
    const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}):/.exec(label);
    const v = values[i];
    if (!m || v === null || v === undefined) return;
    hours.add(m[2]);
    const row = days.get(m[1]) ?? new Map();
    const cell = row.get(m[2]) ?? { sum: 0, n: 0 };
    cell.sum += v; cell.n += 1;
    row.set(m[2], cell);
    days.set(m[1], row);
  });
  if (days.size < 2 || hours.size < 2) return null;
  const cols = [...hours].sort();
  const names = [...days.keys()].sort();
  const step = names.length > MAX_DAY_ROWS ? WEEK : 1;
  const rows: ChartSpec['series'] = [];
  for (let i = 0; i < names.length; i += step) {
    const group = names.slice(i, i + step);
    rows.push({
      name: step === 1 ? group[0] : `week of ${group[0]}`,
      values: cols.map((h) => {
        let sum = 0; let n = 0;
        group.forEach((d) => { const c = days.get(d)!.get(h); if (c) { sum += c.sum; n += c.n; } });
        return n ? sum / n : null;
      }),
    });
  }
  return { chartId: id, title: step === 1 ? title : `${title} (weekly means)`, unit, kind: 'heatmap', x: cols, series: rows };
}
