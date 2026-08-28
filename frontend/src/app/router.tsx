import { BrowserRouter } from "react-router-dom";

import { createApiClient, type SuseoroApi } from "../api/client";
import { App } from "./App";

interface SuseoroRouterProps {
  api?: SuseoroApi;
}

const defaultApi = createApiClient();

export function SuseoroRouter({ api = defaultApi }: SuseoroRouterProps) {
  return (
    <BrowserRouter>
      <App api={api} />
    </BrowserRouter>
  );
}
