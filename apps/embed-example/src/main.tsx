import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ShellErrorBoundary } from "@opendle/ui";
import { App } from "./App.js";
import "@opendle/ui/styles.css";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) throw new Error("The example host root is missing.");

createRoot(root).render(
  <StrictMode>
    <ShellErrorBoundary resetKey="embed-example">
      <App />
    </ShellErrorBoundary>
  </StrictMode>,
);
