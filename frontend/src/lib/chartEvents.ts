import { ChatEvent } from '../api';
import { ChartSpec } from './chartModel';

/**
 * One ChartSpec per chart.show event, for the life of the event.
 *
 * The transcript is re-rendered on every streamed token. A fresh spec each time
 * handed the chart a new object every few milliseconds, so it rebuilt its option and
 * discarded the user's zoom and legend choices. Keyed by the event object, so the
 * same event is the same spec, and a pinned chart in the State panel is the very
 * spec the transcript draws.
 */
const cache = new WeakMap<ChatEvent, ChartSpec>();

export function specFor(event: ChatEvent): ChartSpec {
  let spec = cache.get(event);
  if (!spec) {
    spec = {
      chartId: event.chartId, title: event.title, kind: event.chartKind, unit: event.unit,
      unitRight: event.unitRight, stacked: event.stacked, note: event.note,
      x: event.x ?? [], series: event.series ?? [], thinnedFrom: event.thinnedFrom,
    };
    cache.set(event, spec);
  }
  return spec;
}
