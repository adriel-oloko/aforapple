// app/api/fal/token/route.ts
//
// Issues short-lived JWTs for the realtime WebRTC session.
// fal.realtime.connect() calls GET /api/fal/token before opening the
// WebRTC channel. Must have the same allowedEndpoints as the proxy route
// or the token request returns 400.

import { createRouteHandler } from "@fal-ai/server-proxy/nextjs";

export const { GET, POST, PUT } = createRouteHandler({
	allowedEndpoints: ["decart/lucy-2-5/realtime", "decart/lucy-2-5/**"],
	allowUnauthorizedRequests: true, // set to false and add session auth when you go to production
});
