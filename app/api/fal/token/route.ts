// app/api/fal/token/route.ts
//
// Issues short-lived JWTs for the realtime WebRTC session.
// fal.realtime.connect() -> tokenProvider calls GET /api/fal/token?app=...
// This handler calls fal's REST API directly to obtain a token scoped
// to the requested model, keeping FAL_KEY server-side.

const TOKEN_EXPIRATION_SECONDS = 120;
const REST_API_URL = "https://rest.fal.ai";

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const app = searchParams.get("app");
  if (!app) {
    return Response.json({ error: "Missing app parameter" }, { status: 400 });
  }

  const falKey = process.env.FAL_KEY;
  if (!falKey) {
    return Response.json(
      { error: "FAL_KEY not configured on server" },
      { status: 500 },
    );
  }

  // Parse the owner/alias from the app identifier.
  // e.g. "decart/lucy-2-5/realtime" -> alias "lucy-2-5"
  const parts = app.split("/");
  if (parts.length < 2) {
    return Response.json(
      { error: `Invalid app identifier: ${app}` },
      { status: 400 },
    );
  }
  const alias = parts[1];

  try {
    const res = await fetch(`${REST_API_URL}/tokens/`, {
      method: "POST",
      headers: {
        Authorization: `Key ${falKey}`,
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        allowed_apps: [alias],
        token_expiration: TOKEN_EXPIRATION_SECONDS,
      }),
    });

    if (!res.ok) {
      const errorText = await res.text();
      return Response.json(
        { error: `Token request failed: ${errorText}` },
        { status: res.status },
      );
    }

    // The fal REST /tokens/ endpoint returns the JWT as a JSON-encoded
    // string literal (i.e. the raw token wrapped in quotes: "eyJ...").
    // We must return the *bare* token to the browser; the @fal-ai/client
    // TokenProvider contract expects a plain string. If we pass the
    // quoted value through, the quotes end up URL-encoded as %22 in the
    // WebSocket URL and the connection fails.
    const raw = await res.text();
    let token = raw;
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed === "string") token = parsed;
    } catch {
      // Not JSON — fall back to the raw body, trimmed of stray quotes.
      token = raw.trim().replace(/^"|"$/g, "");
    }

    return new Response(token, {
      headers: { "Content-Type": "text/plain" },
    });
  } catch (err) {
    return Response.json(
      {
        error:
          err instanceof Error ? err.message : "Token request failed",
      },
      { status: 500 },
    );
  }
}
