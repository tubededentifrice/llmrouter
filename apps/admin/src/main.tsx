import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@opendle/ui/styles.css";
import { LocalAdministrationApp } from "./App.js";
import "./styles.css";

const root = document.querySelector<HTMLElement>("#root");
if (root === null) throw new Error("The root element is missing.");
createRoot(root).render(
  <StrictMode>
    <LocalAdministrationApp />
  </StrictMode>,
);
