// app/api/fal/proxy/route.ts
//
// Proxies fal inference requests from the browser, keeping FAL_KEY server-side.
// allowedEndpoints locks this proxy to Lucy 2.5 only — nothing else can be
// called through it. allowUnauthorizedRequests: false rejects any request
// that doesn't come from an authenticated session (wire in your own session
// check via the Next.js middleware or a custom handler wrapper if needed).

import { createRouteHandler } from "@fal-ai/server-proxy/nextjs";

export const { GET, POST, PUT } = createRouteHandler({
  allowedEndpoints: ["decart/lucy-2-5/realtime", "decart/lucy-2-5/**"],
  allowUnauthorizedRequests: true, // set to false and add session auth when you go to production
});