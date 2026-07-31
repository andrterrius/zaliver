import { ToggleSwitch } from "./ToggleSwitch";
import { RangeSlider, type RangeValue } from "./RangeSlider";
import { TextPositionPreview } from "./TextPositionPreview";

export type TextOverlayState = {
  enabled: boolean;
  fromMiddle: boolean;
  afterFrameChange: boolean;
  text: string;
  fontSize: number;
  glowEnabled: boolean;
  glowColor: string;
  textColor: string;
  letterSpacing: number;
  fontBold: boolean;
  fontPath: string;
  waveAmp: RangeValue;
  waveSpeed: RangeValue;
  anchorX: number;
  anchorY: number;
};

export function defaultUniquifyTextOverlay(): TextOverlayState {
  return {
    enabled: true,
    fromMiddle: true,
    afterFrameChange: false,
    text: "GAME IN BIO",
    fontSize: 95,
    glowEnabled: true,
    glowColor: "#00FFFF",
    textColor: "#FFFFFF",
    letterSpacing: 0,
    fontBold: true,
    fontPath: "",
    waveAmp: { lo: 14, hi: 14 },
    waveSpeed: { lo: 9, hi: 9 },
    anchorX: 0.5,
    anchorY: 0.15,
  };
}

export function defaultSliceTextOverlay(): TextOverlayState {
  return {
    enabled: true,
    fromMiddle: true,
    afterFrameChange: false,
    text: "5.000.000$ GIVEAWAY IN BIO",
    fontSize: 58,
    glowEnabled: true,
    glowColor: "#00FFFF",
    textColor: "#FFFFFF",
    letterSpacing: 0,
    fontBold: true,
    fontPath: "",
    waveAmp: { lo: 15, hi: 15 },
    waveSpeed: { lo: 5, hi: 5 },
    anchorX: 0.5,
    anchorY: 0.5,
  };
}

export function defaultStitchTextOverlay(): TextOverlayState {
  return {
    ...defaultSliceTextOverlay(),
    afterFrameChange: false,
  };
}

export function textOverlayToApi(state: TextOverlayState) {
  const ampLo = state.waveAmp.lo / 100;
  const ampHi = state.waveAmp.hi / 100;
  const spdLo = state.waveSpeed.lo / 100;
  const spdHi = state.waveSpeed.hi / 100;
  return {
    enabled: state.enabled,
    text: state.text,
    font_size: state.fontSize,
    glow_enabled: state.glowEnabled,
    glow_color: state.glowColor,
    text_color: state.textColor,
    letter_spacing: state.letterSpacing,
    custom_font_path: state.fontPath.trim(),
    font_bold: state.fontBold,
    orientation: "vertical",
    from_middle: state.fromMiddle,
    after_frame_change: state.afterFrameChange,
    anchor_x: state.anchorX,
    anchor_y: state.anchorY,
    wave_amp_frac: (ampLo + ampHi) * 0.5,
    wave_frame_speed: (spdLo + spdHi) * 0.5,
    wave_amp_frac_min: ampLo,
    wave_amp_frac_max: ampHi,
    wave_frame_speed_min: spdLo,
    wave_frame_speed_max: spdHi,
  };
}

type Props = {
  value: TextOverlayState;
  onChange: (next: TextOverlayState) => void;
  showAfterFrameChange?: boolean;
};

export function TextOverlayFields({
  value,
  onChange,
  showAfterFrameChange = false,
}: Props) {
  const patch = (partial: Partial<TextOverlayState>) =>
    onChange({ ...value, ...partial });

  return (
    <section className="group stack">
      <h3 className="group-title">Текст на видео</h3>
      <ToggleSwitch
        label="Добавить текст"
        checked={value.enabled}
        onChange={(enabled) => patch({ enabled })}
      />
      {value.enabled ? (
        <div className="stack">
          <label className="check">
            <input
              type="checkbox"
              checked={value.fromMiddle}
              onChange={(e) => patch({ fromMiddle: e.target.checked })}
            />
            Текст с середины видео до конца
          </label>
          {showAfterFrameChange ? (
            <label className="check">
              <input
                type="checkbox"
                checked={value.afterFrameChange}
                onChange={(e) => patch({ afterFrameChange: e.target.checked })}
              />
              Текст после смены кадра
            </label>
          ) : null}
          <textarea
            className="field"
            style={{ minHeight: 72, fontFamily: "inherit", fontSize: 13 }}
            value={value.text}
            onChange={(e) => patch({ text: e.target.value })}
            placeholder="Текст для наложения…"
          />
          <div className="form-grid">
            <label className="hint">Размер</label>
            <input
              className="field"
              type="number"
              min={12}
              max={240}
              value={value.fontSize}
              onChange={(e) =>
                patch({ fontSize: Math.max(12, Math.min(240, Number(e.target.value) || 12)) })
              }
            />
            <label className="hint">Свечение</label>
            <div className="row">
              <label className="check">
                <input
                  type="checkbox"
                  checked={value.glowEnabled}
                  onChange={(e) => patch({ glowEnabled: e.target.checked })}
                />
                Включено
              </label>
              <input
                className="field color-field"
                type="color"
                value={value.glowColor}
                onChange={(e) => patch({ glowColor: e.target.value })}
                title="Цвет неона"
              />
            </div>
            <label className="hint">Цвет</label>
            <input
              className="field color-field"
              type="color"
              value={value.textColor}
              onChange={(e) => patch({ textColor: e.target.value })}
              title="Цвет текста"
            />
            <label className="hint">Межбуквенный интервал</label>
            <div className="row">
              <input
                className="field"
                style={{ width: 100 }}
                type="number"
                min={-20}
                max={80}
                value={value.letterSpacing}
                onChange={(e) =>
                  patch({
                    letterSpacing: Math.max(
                      -20,
                      Math.min(80, Number(e.target.value) || 0),
                    ),
                  })
                }
              />
              <span className="hint">px</span>
            </div>
            <label className="hint">Шрифт</label>
            <div className="stack" style={{ gap: 6 }}>
              <label className="check">
                <input
                  type="checkbox"
                  checked={value.fontBold}
                  onChange={(e) => patch({ fontBold: e.target.checked })}
                />
                Жирный
              </label>
              <input
                className="field"
                value={value.fontPath}
                onChange={(e) => patch({ fontPath: e.target.value })}
                placeholder="Путь к файлу шрифта (опционально)"
              />
            </div>
            <label className="hint">Волна — амплитуда</label>
            <RangeSlider
              min={0}
              max={35}
              step={1}
              decimals={0}
              suffix=" %"
              value={value.waveAmp}
              onChange={(waveAmp) => patch({ waveAmp })}
            />
            <label className="hint">Волна — скорость</label>
            <RangeSlider
              min={0}
              max={25}
              step={1}
              decimals={0}
              value={value.waveSpeed}
              onChange={(waveSpeed) => patch({ waveSpeed })}
            />
          </div>
          <div className="row">
            <button
              type="button"
              className="btn secondary"
              onClick={() => patch({ anchorX: 0.5 })}
            >
              По центру (горизонт.)
            </button>
            <button
              type="button"
              className="btn secondary"
              onClick={() => patch({ anchorY: 0.5 })}
            >
              По центру (вертик.)
            </button>
          </div>
          <TextPositionPreview
            anchorX={value.anchorX}
            anchorY={value.anchorY}
            text={value.text}
            textColor={value.textColor}
            glowColor={value.glowColor}
            glowEnabled={value.glowEnabled}
            letterSpacing={value.letterSpacing}
            fontSize={value.fontSize}
            fontBold={value.fontBold}
            onChange={(anchorX, anchorY) => patch({ anchorX, anchorY })}
          />
          <p className="hint">
            Клик/перетаскивание задаёт позицию текста (anchor{" "}
            {value.anchorX.toFixed(2)}, {value.anchorY.toFixed(2)}).
          </p>
        </div>
      ) : null}
    </section>
  );
}
