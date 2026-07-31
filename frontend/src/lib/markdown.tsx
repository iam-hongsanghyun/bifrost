/**
 * Just enough markdown for what the agent actually writes.
 *
 * Result tables were rendering as literal `| metric | value |` pipes, which is the
 * agent's most useful output arriving in its least readable form.
 *
 * This builds React elements rather than HTML. That is the whole design: rendering
 * model output through `innerHTML` means every reply is an injection surface, and
 * the usual answer — a parser plus a sanitiser — is two dependencies and a policy to
 * get wrong. Constructing elements makes the unsafe path unreachable instead of
 * guarded, and covers what the agent emits: tables, headings, lists, fenced code,
 * inline code, bold, italic and links.
 *
 * Deliberately NOT supported: raw HTML (dropped, not escaped-and-shown), images,
 * footnotes, nested blockquotes. If the agent starts leaning on something missing,
 * add it here rather than reaching for a library — the constraint is what keeps this
 * safe.
 */
import { Fragment, ReactNode } from 'react';

/** Inline spans: `code`, **bold**, *italic*, [text](url). */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  // One pass, longest-delimiter-first so ** is not eaten by *.
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0];
    const key = `${keyPrefix}-i${i++}`;
    if (token.startsWith('`')) {
      out.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('**')) {
      out.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith('*')) {
      out.push(<em key={key}>{token.slice(1, -1)}</em>);
    } else {
      const split = token.indexOf('](');
      const label = token.slice(1, split);
      const href = token.slice(split + 2, -1);
      // Only http(s): a model-supplied `javascript:` or `data:` URL is not a link.
      const safe = /^https?:\/\//i.test(href);
      out.push(safe ? <a key={key} href={href} target="_blank" rel="noreferrer">{label}</a> : label);
    }
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function isTableSeparator(line: string): boolean {
  return /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes('-');
}

function cells(row: string): string[] {
  return row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
}

/** Render a markdown block into React elements. */
export function Markdown({ text }: { text: string }) {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code.
    if (/^\s*```/.test(line)) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { body.push(lines[i]); i += 1; }
      i += 1;
      blocks.push(<pre key={`b${blocks.length}`} className="md-pre"><code>{body.join('\n')}</code></pre>);
      continue;
    }

    // Table: a header row followed by a separator.
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(cells(lines[i]));
        i += 1;
      }
      blocks.push(
        <div key={`b${blocks.length}`} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>{head.map((h, c) => <th key={c}>{inline(h, `h${c}`)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>{row.map((cell, c) => <td key={c}>{inline(cell, `c${r}-${c}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // Heading.
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const content = inline(heading[2], `h${blocks.length}`);
      blocks.push(
        level <= 2
          ? <h3 key={`b${blocks.length}`} className="md-h">{content}</h3>
          : <h4 key={`b${blocks.length}`} className="md-h">{content}</h4>,
      );
      i += 1;
      continue;
    }

    // List (bulleted or numbered) — a run of adjacent items.
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ''));
        i += 1;
      }
      const rendered = items.map((item, n) => <li key={n}>{inline(item, `l${n}`)}</li>);
      blocks.push(
        ordered
          ? <ol key={`b${blocks.length}`} className="md-list">{rendered}</ol>
          : <ul key={`b${blocks.length}`} className="md-list">{rendered}</ul>,
      );
      continue;
    }

    // Blank line: a paragraph break, not an empty element.
    if (line.trim() === '') { i += 1; continue; }

    // Paragraph — consecutive non-special lines, joined.
    const para: string[] = [];
    while (
      i < lines.length && lines[i].trim() !== ''
      && !/^\s*```/.test(lines[i]) && !/^(#{1,4})\s/.test(lines[i])
      && !/^\s*([-*+]|\d+\.)\s+/.test(lines[i])
      && !(lines[i].includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1]))
    ) { para.push(lines[i]); i += 1; }
    blocks.push(<p key={`b${blocks.length}`} className="md-p">{inline(para.join('\n'), `p${blocks.length}`)}</p>);
  }

  return <Fragment>{blocks}</Fragment>;
}
