/**
 * Handing the user's own data to the agent.
 *
 * The backend has stored uploads and offered a `read_attachment` tool from the
 * start — but nothing in the UI could reach the upload endpoint, so the agent had
 * a tool for data the user had no way to give it. Which left it synthesising
 * profiles instead, and a synthesised wind series is the most dangerous input in
 * a model: unlike a lone scalar it LOOKS like measured data once it sits in a
 * sheet.
 *
 * Two pieces live here because both the composer and App need them: how a file is
 * described to the user, and how it is described to the agent. Those are
 * deliberately different — the agent needs the stored path, and the user needs
 * not to look at one.
 */
import { UploadedFile } from '../api';

export function prettyBytes(size: number): string {
  if (size >= 1e9) return `${(size / 1e9).toFixed(1)} GB`;
  if (size >= 1e6) return `${(size / 1e6).toFixed(1)} MB`;
  if (size >= 1e3) return `${Math.round(size / 1e3)} kB`;
  return `${size} B`;
}

/**
 * The block appended to the message the AGENT sees.
 *
 * `read_attachment` takes a stored path, so the paths must arrive in the message
 * itself; the upload endpoint alone leaves the agent with no idea a file exists.
 */
export function attachmentBlockFor(files: UploadedFile[]): string {
  if (files.length === 0) return '';
  const lines = files.map((f) => `- ${f.name} → ${f.path} (${prettyBytes(f.size)})`);
  return `\n\n[Attached files — read them with read_attachment]\n${lines.join('\n')}`;
}
