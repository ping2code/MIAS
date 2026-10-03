import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { applyTheme, readThemePreference } from "./lib/theme";
import "./styles/app.css";

// Apply the stored theme before the first render, so the first painted content already uses it.
applyTheme(readThemePreference());

const root = document.getElementById("root");
if (root === null) throw new Error("#root element missing");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
