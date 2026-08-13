import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.js";

const root = document.querySelector<HTMLElement>("#root");
if (root === null) throw new Error("The root element is missing.");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
