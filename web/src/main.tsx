import { createRoot } from "react-dom/client";
import App from "./App";
import { ToastProvider } from "./components/ui";
import "./styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("NovaMarket root element is missing");
createRoot(container).render(
  <ToastProvider>
    <App />
  </ToastProvider>,
);
