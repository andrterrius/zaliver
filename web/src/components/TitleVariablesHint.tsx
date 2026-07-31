import { useEffect, useState } from "react";
import { api, type TitleVariable } from "../api/client";

type Props = {
  onInsert: (token: string) => void;
};

export function TitleVariablesHint({ onInsert }: Props) {
  const [open, setOpen] = useState(false);
  const [vars, setVars] = useState<TitleVariable[]>([]);
  const [example, setExample] = useState("");

  useEffect(() => {
    if (!open || vars.length) return;
    void api
      .titleVariables()
      .then((r) => {
        setVars(r.variables);
        setExample(r.example);
      })
      .catch(() => undefined);
  }, [open, vars.length]);

  return (
    <>
      <button
        type="button"
        className="btn secondary"
        style={{ padding: "2px 8px", fontSize: 12 }}
        onClick={() => setOpen(true)}
      >
        Переменные…
      </button>
      {open ? (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div
            className="modal-card stack"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="page-header">
              <h3 className="group-title">Переменные</h3>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setOpen(false)}
              >
                Закрыть
              </button>
            </div>
            <p className="hint">Пример: {example}</p>
            <div className="list-panel">
              {vars.map((v) => (
                <button
                  key={v.token}
                  type="button"
                  className="list-item"
                  style={{
                    width: "100%",
                    textAlign: "left",
                    background: "transparent",
                    border: "none",
                  }}
                  onClick={() => {
                    onInsert(v.token);
                    setOpen(false);
                  }}
                >
                  <div style={{ fontWeight: 600, color: "var(--accent-soft)" }}>
                    {v.token}
                  </div>
                  <div className="hint">
                    {v.description} · напр. {v.example}
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
