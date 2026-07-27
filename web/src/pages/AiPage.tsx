export function AiPage() {
  return (
    <div className="stack">
      <h1 className="title">ИИ</h1>
      <p className="hint">
        Промпты и чат пока доступны в десктопном приложении. Ключ API и модель
        настраиваются во вкладке «Настройки».
      </p>
      <section className="group">
        <h3 className="group-title">Подсказка</h3>
        <p className="hint">
          Веб-чат и генерация названий/описаний появятся после выноса AI-воркеров
          в core API.
        </p>
      </section>
    </div>
  );
}
