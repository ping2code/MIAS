import { useId, useState } from "react";
import { applyTheme, readThemePreference, writeThemePreference, type ThemePreference } from "../lib/theme";

const OPTIONS: readonly { value: ThemePreference; label: string; icon: string }[] = [
  { value: "system", label: "System", icon: "◐" },
  { value: "light", label: "Light", icon: "☀" },
  { value: "dark", label: "Dark", icon: "☾" },
];

/** Theme: native radio buttons (arrow keys move between them), each with an icon and text. */
export function ThemeControl() {
  const name = useId();
  const [value, setValue] = useState<ThemePreference>(readThemePreference);
  const choose = (next: ThemePreference): void => {
    setValue(next);
    writeThemePreference(next);
    applyTheme(next);
  };
  return (
    <fieldset className="theme-control">
      <legend className="visually-hidden-text">Theme</legend>
      {OPTIONS.map((o) => (
        <label key={o.value} className="theme-option" title={`${o.label} theme`}>
          <input
            type="radio"
            name={name}
            value={o.value}
            checked={value === o.value}
            onChange={() => {
              choose(o.value);
            }}
          />
          <span aria-hidden="true" className="theme-icon">
            {o.icon}
          </span>
          <span className="theme-label">{o.label}</span>
        </label>
      ))}
    </fieldset>
  );
}
