// main.tsx — the browser entry point.
// Owns: mounting <App /> into #root and pulling in the base stylesheet; nothing else.

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
