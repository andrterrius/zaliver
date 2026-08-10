import { useEffect } from "react";

/**
 * Колёсико мыши не меняет значение в focused `input[type=number]`.
 * Страницу при этом всё равно можно скроллить (blur + preventDefault).
 */
export function useNoWheelNumberInputs(): void {
  useEffect(() => {
    const onWheel = (event: WheelEvent) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) return;
      if (target.type !== "number") return;
      if (document.activeElement !== target) return;
      target.blur();
      event.preventDefault();
    };
    document.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => {
      document.removeEventListener("wheel", onWheel, true);
    };
  }, []);
}
