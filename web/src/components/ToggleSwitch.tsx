type Props = {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
};

export function ToggleSwitch({ label, checked, onChange, disabled }: Props) {
  return (
    <label className={`toggle ${disabled ? "disabled" : ""}`}>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        className={`toggle-track ${checked ? "on" : ""}`}
        disabled={disabled}
        onClick={() => onChange(!checked)}
      >
        <span className="toggle-thumb" />
      </button>
      <span>{label}</span>
    </label>
  );
}
