import { createRoot } from "react-dom/client";
import { App } from "../../src/App.tsx";

const root = document.getElementById("root");
if (root === null) throw Error("Missing fixture root.");
createRoot(root).render(<App />);
