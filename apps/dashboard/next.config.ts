import type { NextConfig } from "next";

// A static export: the dashboard is plain HTML/JS served by any web server and
// talks only to the operator API from the browser. There is no Node server
// holding the operator token, and nothing here can reach a broker.
const config: NextConfig = {
  output: "export",
  trailingSlash: true,
  reactStrictMode: true,
  poweredByHeader: false,
  images: { unoptimized: true },
};

export default config;
