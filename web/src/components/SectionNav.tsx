type Props = {
  sections: string[];
  active: number;
  onChange: (index: number) => void;
};

export function SectionNav({ sections, active, onChange }: Props) {
  return (
    <div className="section-nav" role="tablist">
      {sections.map((label, i) => (
        <button
          key={label}
          type="button"
          role="tab"
          aria-selected={active === i}
          className={`section-nav-btn ${active === i ? "active" : ""}`}
          onClick={() => onChange(i)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
