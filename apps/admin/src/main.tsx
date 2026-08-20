import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@opendle/ui/styles.css";
import { LocalAdministrationApp } from "./App.js";
import { EmbedFrame, InvalidEmbedFrame } from "./EmbedFrame.js";
import { embedFrameParameters } from "./embedProtocol.js";
import "./embedStyles.css";
import "./styles.css";

const root = document.querySelector<HTMLElement>("#root");
if (root === null) throw new Error("The root element is missing.");
const embedPath = window.location.pathname === "/service-administration";
const embed = embedPath ? embedFrameParameters() : null;
if (embedPath) document.documentElement.dataset.embedFrame = "true";
createRoot(root).render(
  <StrictMode>
    {embedPath ? (
      embed === null ? (
        <InvalidEmbedFrame />
      ) : (
        <EmbedFrame sessionId={embed.sessionId} hostOrigin={embed.hostOrigin} />
      )
    ) : (
      <LocalAdministrationApp />
    )}
  </StrictMode>,
);
