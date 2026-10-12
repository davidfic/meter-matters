// Cloudflare Email Worker: hands each forwarded bill to the app.
//
// Email Routing on the domain receives mail for bills-<token>@<domain>; a
// catch-all route sends every message here, and this posts it, unchanged, to
// the app's inbound endpoint. The app checks the shared secret and files the
// bill against the address it was sent to. Nothing is stored here.
//
// Set up, in the Cloudflare dashboard:
//   1. Email > Email Routing > enable it on the zone (adds the MX records).
//   2. Workers & Pages > Create > paste this file. Name it, say, bills-inbound.
//   3. The worker's Settings > Variables: add a secret  INBOUND_SECRET  with the
//      value of elec-inbound-secret from Secret Manager, and a variable
//      APP_URL = https://metermatters.app
//   4. Email Routing > Routing rules > Catch-all: action "Send to a Worker",
//      pick the worker, and enable it.
//
// A message the app refuses (bad secret, not for us) is bounced; the app
// answers 200 for anything it accepted, including addresses it does not know,
// so nobody can probe for real ones by watching bounces.

export default {
  async email(message, env) {
    const raw = await new Response(message.raw).arrayBuffer();
    const res = await fetch(`${env.APP_URL}/api/inbound/email`, {
      method: "POST",
      headers: {
        "Content-Type": "message/rfc822",
        "Authorization": `Bearer ${env.INBOUND_SECRET}`,
        "X-Envelope-To": message.to,
        "X-Envelope-From": message.from,
      },
      body: raw,
    });
    if (!res.ok) {
      message.setReject(`Not accepted (${res.status})`);
    }
  },
};
