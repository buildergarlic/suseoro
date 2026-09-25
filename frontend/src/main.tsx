import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { SimpleLibraryApp } from "./simple/SimpleLibraryApp";
import "./simple/simple.css";
import "./simple/workspace-layout.css";

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("수서로를 표시할 화면을 찾지 못했습니다.");
}

createRoot(rootElement).render(
  <StrictMode>
    <SimpleLibraryApp />
  </StrictMode>,
);
