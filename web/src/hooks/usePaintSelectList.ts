import { useCallback, useEffect, useRef, type PointerEvent as ReactPointerEvent } from "react";

type DragState = {
  active: boolean;
  paintSelect: boolean;
  lastKey: string | null;
  pointerId: number | null;
};

/**
 * Paint-select rows while dragging (checkbox column or whole row).
 * Uses row geometry hit-testing — reliable over checkboxes / pointer capture.
 */
export function usePaintSelectList(opts: {
  /** Current selection for deciding paint vs erase on pointerdown. */
  isSelected: (key: string) => boolean;
  /** Apply paint mode to a row key. */
  paint: (key: string, select: boolean) => void;
  /** Attribute holding the selection key, default data-entry-path. */
  attr?: string;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState>({
    active: false,
    paintSelect: true,
    lastKey: null,
    pointerId: null,
  });
  const isSelectedRef = useRef(opts.isSelected);
  const paintRef = useRef(opts.paint);
  isSelectedRef.current = opts.isSelected;
  paintRef.current = opts.paint;
  const attr = opts.attr || "data-entry-path";

  const endDrag = useCallback(() => {
    const d = dragRef.current;
    d.active = false;
    d.lastKey = null;
    d.pointerId = null;
  }, []);

  const keyAtPoint = useCallback(
    (clientX: number, clientY: number): string | null => {
      const list = listRef.current;
      if (!list) return null;
      const rows = list.querySelectorAll<HTMLElement>(`[${attr}]`);
      for (const row of rows) {
        const r = row.getBoundingClientRect();
        if (
          clientY >= r.top &&
          clientY < r.bottom &&
          clientX >= r.left &&
          clientX < r.right
        ) {
          return row.getAttribute(attr);
        }
      }
      return null;
    },
    [attr],
  );

  const paintAtPoint = useCallback(
    (clientX: number, clientY: number) => {
      const d = dragRef.current;
      if (!d.active) return;
      const key = keyAtPoint(clientX, clientY);
      if (!key || key === d.lastKey) return;
      d.lastKey = key;
      paintRef.current(key, d.paintSelect);
    },
    [keyAtPoint],
  );

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      if (!dragRef.current.active) return;
      paintAtPoint(e.clientX, e.clientY);
    };
    const onUp = () => endDrag();
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [endDrag, paintAtPoint]);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const t = e.target as HTMLElement;
    if (t.closest(".source-browser-open")) return;
    const key = keyAtPoint(e.clientX, e.clientY);
    if (!key) return;
    const paintSelect = !isSelectedRef.current(key);
    dragRef.current = {
      active: true,
      paintSelect,
      lastKey: key,
      pointerId: e.pointerId,
    };
    paintRef.current(key, paintSelect);
    e.preventDefault();
    e.stopPropagation();
  };

  return { listRef, onPointerDown };
}
