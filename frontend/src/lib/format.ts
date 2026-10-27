/** One escaper, one number formatter, one download helper — shared by every chart and table. */

const HTML_ESCAPES: Record<string, string> = {
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
};

/**
 * Escape text bound for an HTML string.
 *
 * ECharts tooltips are HTML, and series names, x labels and units come from the
 * agent (and through it from files the user attached). Unescaped, a series named
 * `<img src=x onerror=...>` runs script in the app's origin.
 */
export function escapeHtml(text: unknown): string {
  return String(text ?? '').replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

/** A number for reading: enough digits to tell neighbours apart, no more. */
export function fmtNumber(value: number | null | undefined, digits?: number): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '';
  const abs = Math.abs(value);
  const max = digits ?? (abs >= 1000 ? 0 : abs >= 100 ? 1 : abs >= 1 ? 2 : abs === 0 ? 0 : 3);
  const text = value.toLocaleString(undefined, { maximumFractionDigits: max });
  // A value that rounds to nothing is 0, not -0 (solver output is full of -0.0).
  return /^-0(\.0*)?$/.test(text) ? text.slice(1) : text;
}

/** Axis ticks: 12,500 -> 12.5k. */
export function fmtCompact(value: number): string {
  if (!Number.isFinite(value)) return '';
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${+(value / 1e9).toFixed(2)}G`;
  if (abs >= 1e6) return `${+(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e4) return `${+(value / 1e3).toFixed(1)}k`;
  return String(+value.toFixed(abs >= 100 ? 0 : abs >= 1 ? 2 : 3));
}

/** Save text as a file. Revokes the object URL so repeated exports do not leak. */
export function downloadText(filename: string, text: string, mime = 'text/csv;charset=utf-8'): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  downloadUrl(filename, url);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function downloadUrl(filename: string, url: string): void {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function safeFilename(title: string, fallback: string): string {
  return (title || fallback).replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '') || fallback;
}

/** One CSV cell. A leading = + - @ would be read as a formula by a spreadsheet. */
export function csvCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  let text = typeof value === 'number' ? String(value) : String(value);
  if (typeof value === 'string' && /^[=+\-@]/.test(text)) text = `'${text}`;
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}
