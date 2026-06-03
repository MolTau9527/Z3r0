import { renderToString } from "react-dom/server";
import { LandingContent } from "./features/landing/LandingContent";

const quickstartUrl = "https://github.com/yv1ing/Z3r0/blob/main/QUICKSTART.md";

export function renderLandingHtml(logoSrc: string) {
  return renderToString(
    <LandingContent logoSrc={logoSrc} primaryAction={{ label: "Read quickstart", href: quickstartUrl, external: true }} />,
  );
}
