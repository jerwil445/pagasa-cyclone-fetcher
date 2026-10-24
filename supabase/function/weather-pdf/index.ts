// Custom reader-token auth. Never return the privileged key or accept a URL.
Deno.serve(async (req: Request) => {
  const reply = (status: number, body: object) => new Response(JSON.stringify(body), {
    status, headers: {"Content-Type": "application/json", "Cache-Control": "no-store"},
  });
  if (req.method !== "POST") return reply(405, {error: "POST required"});
  try {
    const reader = req.body?.getReader();
    if (!reader) return reply(400, {error: "Missing body"});
    let bytes = new Uint8Array();
    while (true) {
      const {done, value} = await reader.read(); if (done) break;
      if (bytes.length + value.length > 2048) { await reader.cancel(); return reply(413, {error: "Too large"}); }
      const next = new Uint8Array(bytes.length + value.length); next.set(bytes); next.set(value, bytes.length); bytes = next;
    }
    const {token, sha256} = JSON.parse(new TextDecoder().decode(bytes));
    if (typeof token !== "string" || token.length < 40 || token.length > 512 ||
        typeof sha256 !== "string" || !/^[a-f0-9]{64}$/.test(sha256)) return reply(403, {error: "Access denied"});
    const base = Deno.env.get("SUPABASE_URL")!;
    const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const headers = {apikey: key, Authorization: `Bearer ${key}`, "Content-Type": "application/json"};
    const allowed = await fetch(`${base}/rest/v1/rpc/weather_pdf_access`, {
      method: "POST", headers, body: JSON.stringify({p_token: token, p_sha: sha256}), redirect: "error",
    });
    if (!allowed.ok || await allowed.json() !== true) return reply(403, {error: "Access denied"});
    const signed = await fetch(`${base}/storage/v1/object/sign/pagasa-pdfs/${sha256}.pdf`, {
      method: "POST", headers, body: JSON.stringify({expiresIn: 60}), redirect: "error",
    });
    if (!signed.ok) return reply(503, {error: "PDF unavailable"});
    const result = await signed.json();
    return reply(200, {signed_path: result.signedURL});
  } catch { return reply(503, {error: "PDF unavailable"}); }
});
