"use client";
import { useCallback, useEffect, useRef } from "react";

/**
 * 三栏之间的拖拽分隔线。
 *
 * 为什么不用现成的 split-pane 库：这里只需要「一根竖线把宽度交给父组件」，
 * 引一个库还要跟着它的受控/非受控模型走。自己实现反而更可控，尤其是：
 *   - 拖拽时给 body 加 col-resize 并禁掉 iframe 的指针事件（否则鼠标划过右栏时事件被吃掉）
 *   - 提供键盘操作（←/→ 微调，Home/End 到极限，回车复位），纯鼠标交互对无障碍不友好
 */
export default function Splitter({
  value,
  min,
  max,
  side,
  label,
  onChange,
  onReset,
}: {
  value: number;
  min: number;
  max: number;
  /** left = 拖动它改变左侧宽度；right = 改变右侧宽度 */
  side: "left" | "right";
  label: string;
  onChange: (next: number) => void;
  onReset: () => void;
}) {
  const startRef = useRef<{ x: number; v: number } | null>(null);

  const clamp = useCallback((v: number) => Math.min(max, Math.max(min, v)), [min, max]);

  useEffect(() => {
    return () => {
      document.body.classList.remove("ws-resizing");
    };
  }, []);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    startRef.current = { x: e.clientX, v: value };
    document.body.classList.add("ws-resizing");
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const s = startRef.current;
    if (!s) return;
    const dx = e.clientX - s.x;
    onChange(clamp(side === "left" ? s.v + dx : s.v - dx));
  };

  const end = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!startRef.current) return;
    startRef.current = null;
    document.body.classList.remove("ws-resizing");
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* 指针已释放（比如拖出窗口）时忽略 */
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 48 : 16;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      onChange(clamp(side === "left" ? value - step : value + step));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      onChange(clamp(side === "left" ? value + step : value - step));
    } else if (e.key === "Home") {
      e.preventDefault();
      onChange(min);
    } else if (e.key === "End") {
      e.preventDefault();
      onChange(max);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onReset();
    }
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={Math.round(value)}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      title={`${label}（拖动调整，双击复位）`}
      className="ws-splitter"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
      onDoubleClick={onReset}
      onKeyDown={onKeyDown}
    >
      <span className="ws-splitter-grip" aria-hidden />
    </div>
  );
}
