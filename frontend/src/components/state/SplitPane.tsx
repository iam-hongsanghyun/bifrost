/**
 * The draggable divider between the conversation and the State panel.
 *
 * Pointer-captured so a fast drag over the chart canvas or the iframe cannot lose the
 * handle, keyboard-adjustable (arrows, Home resets) and double-click resets. The
 * width it reports is the panel's; the caller clamps and persists it.
 */
import { useRef } from 'react';

interface Props {
  width: number;
  min: number;
  max: number;
  onChange: (width: number) => void;
  onReset: () => void;
}

const KEY_STEP = 24;

export function SplitPane({ width, min, max, onChange, onReset }: Props) {
  const start = useRef<{ x: number; w: number } | null>(null);
  const clamp = (w: number) => Math.round(Math.min(max, Math.max(min, w)));
  return (
    <div className="splitter" role="separator" aria-orientation="vertical" tabIndex={0}
      aria-label="Resize the State panel" aria-valuenow={width} aria-valuemin={min} aria-valuemax={max}
      onPointerDown={(e) => { start.current = { x: e.clientX, w: width }; e.currentTarget.setPointerCapture(e.pointerId); }}
      onPointerMove={(e) => { if (start.current) onChange(clamp(start.current.w - (e.clientX - start.current.x))); }}
      onPointerUp={() => { start.current = null; }}
      onDoubleClick={onReset}
      onKeyDown={(e) => {
        if (e.key === 'ArrowLeft') { e.preventDefault(); onChange(clamp(width + KEY_STEP)); }
        if (e.key === 'ArrowRight') { e.preventDefault(); onChange(clamp(width - KEY_STEP)); }
        if (e.key === 'Home') { e.preventDefault(); onReset(); }
      }} />
  );
}
