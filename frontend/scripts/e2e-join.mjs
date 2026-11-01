#!/usr/bin/env node
/**
 * Browser check of the emailed staff-invitation link against a running
 * frontend (`next start` or the container). The invite API is stubbed in the
 * browser, so this proves the page hands the token from the URL to the
 * invite lookup; it does not prove the backend accepts it (backend tests do).
 *
 * Usage: node scripts/e2e-join.mjs [baseUrl]
 */
import { chromium } from "playwright";

const BASE = process.argv[2] ?? "http://127.0.0.1:3000";
const INVITE = {
  email: "asha@acme.test",
  full_name: "Asha Rao",
  role: "ceo",
  company_name: "Acme",
  invited_by_name: "Ravi",
  expires_at: "2099-01-01T00:00:00Z",
  status: "pending",
};

const CASES = [
  { name: "emailed link", query: "?invite=tok123", token: "tok123" },
  { name: "repeated key takes the first", query: "?invite=tok123&invite=other", token: "tok123" },
  { name: "legacy invite_code", query: "?invite_code=tok123", token: "tok123" },
  { name: "no token", query: "", token: null },
];

const browser = await chromium.launch();
const failures = [];
for (const c of CASES) {
  const page = await browser.newPage();
  const seen = [];
  await page.route("**/api/v1/companies/invites/**", (route) => {
    seen.push(decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop()));
    return route.fulfill({ json: INVITE });
  });
  await page.goto(`${BASE}/join${c.query}`);
  // First paint is the "checking" skeleton; wait for the page to settle on
  // either the invitation or the incomplete-link message.
  const settled = page.getByText(c.token ? "Join Acme" : "This invitation link is incomplete");
  await settled.first().waitFor({ timeout: 60000 }).catch(() => {});
  const incomplete = await page.getByText("This invitation link is incomplete").count();
  const joined = await page.getByText("Join Acme").count();
  if (c.token) {
    if (!joined || incomplete || seen[0] !== c.token) {
      failures.push(`${c.name}: rendered=${joined} incomplete=${incomplete} lookups=${seen.length}`);
    }
  } else if (!incomplete || seen.length) {
    failures.push(`${c.name}: expected the incomplete message and no lookup`);
  }
  await page.close();
}
await browser.close();
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("join e2e: all cases passed");
