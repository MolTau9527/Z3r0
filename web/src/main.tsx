import "@douyinfe/semi-ui/react19-adapter";
import React from "react";
import ReactDOM from "react-dom/client";
import "@douyinfe/semi-ui/lib/es/_base/base.css";
import { App } from "./app/App";
import "./app/styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
