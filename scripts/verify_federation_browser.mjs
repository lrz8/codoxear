// Docker only. Run --worker-fixture in a worker container (hostname "worker"),
// then run this script in a "hub" container on the same isolated Docker network.
// Both use the real Pi CLI and Codoxear; the model endpoint is a local fixture.
import { existsSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import assert from "node:assert/strict";
import { chromium } from "/usr/local/lib/node_modules/playwright/index.mjs";

if (!existsSync("/.dockerenv")) throw new Error("Federation verification requires Docker isolation");
const workerMode = process.argv.includes("--worker-fixture");
const agent = "/root/.pi/agent";
const sourceId = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const cwd = "/data/remote-project";
const usage = { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const assistant = { role: "assistant", api: "openai-completions", provider: "completions", model: "shared", usage, timestamp: 2 };
const transcript = [
  { type: "session", version: 3, id: sourceId, cwd, timestamp: "2026-10-14T00:00:00Z" },
  { type: "model_change", id: "model", parentId: null, provider: "completions", modelId: "shared" },
  { type: "message", id: "user", parentId: "model", message: { role: "user", content: [{ type: "text", text: "original-remote-question" }], timestamp: 1 } },
  { type: "message", id: "toolcall", parentId: "user", message: { ...assistant, stopReason: "toolUse", content: [{ type: "toolCall", id: "call_fixture", name: "bash", arguments: { command: "pwd" } }] } },
  { type: "message", id: "toolresult", parentId: "toolcall", message: { role: "toolResult", toolCallId: "call_fixture", toolName: "bash", content: [{ type: "text", text: "original-tool-result" }], isError: false, timestamp: 3 } },
  { type: "message", id: "reply", parentId: "toolresult", message: { ...assistant, stopReason: "stop", content: [{ type: "text", text: "original-remote-answer" }] } },
  { type: "session_info", id: "name", parentId: "reply", name: "Federated conversation" },
].map(row => JSON.stringify(row)).join("\n") + "\n";
const model = { id: "shared", name: "Shared", reasoning: false, input: ["text"], contextWindow: 100000, maxTokens: 1000, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
mkdirSync(agent, { recursive: true });
writeFileSync(agent + "/auth.json", "{}");
writeFileSync(agent + "/settings.json", JSON.stringify({ defaultProvider: "completions", defaultModel: "shared", defaultThinkingLevel: "off" }));
writeFileSync(agent + "/models.json", JSON.stringify({ providers: { completions: {
  api: "openai-completions", baseUrl: "http://hub:19999/v1", apiKey: "fixture-key", models: [model, { ...model, id: "second" }],
} } }));
const folder = agent + "/sessions/" + (workerMode ? "" : "worker/") + "--data-remote-project--";
mkdirSync(folder, { recursive: true });
writeFileSync(folder + "/native.jsonl", transcript);
if (workerMode) {
  mkdirSync(cwd, { recursive: true });
  console.log("Worker fixture ready");
  process.exit(0);
}

const config = "/root/.config/codoxear";
mkdirSync(config, { recursive: true });
writeFileSync(config + "/worker-password", "test-password", { mode: 0o600 });
const peer = { id: "nuc", name: "NUC test", hostname: "worker", url: "http://worker:19644", password_file: config + "/worker-password" };
writeFileSync(config + "/peers.json", JSON.stringify({ peers: [peer] }));
let providerRequest;
const provider = createServer(async (req, res) => {
  let text = "";
  for await (const chunk of req) text += chunk;
  providerRequest = JSON.parse(text);
  res.writeHead(200, { "content-type": "text/event-stream" });
  for (const chunk of [
    { index: 0, delta: { role: "assistant", content: "remote-continuation-ok" }, finish_reason: null },
    { index: 0, delta: {}, finish_reason: "stop" },
  ]) res.write("data: " + JSON.stringify({ id: "fixture", object: "chat.completion.chunk", choices: [chunk] }) + "\n\n");
  res.end("data: [DONE]\n\n");
});
await new Promise(resolve => provider.listen(19999, "0.0.0.0", resolve));
const server = spawn("python3", ["-m", "codoxear.server"], { env: { ...process.env, CODEX_WEB_PASSWORD: "test-password", CODEX_WEB_HOST: "127.0.0.1", CODEX_WEB_PORT: "19643", CODEX_WEB_DEFAULT_AGENT_BACKEND: "pi", CODOXEAR_PI_PACKAGE_ROOT: "/usr/local/lib/node_modules/@earendil-works/pi-coding-agent" }, stdio: ["ignore", "ignore", "inherit"] });
let browser, page;
try {
  for (let n = 0; n < 40; n++) {
    try { if ((await fetch("http://127.0.0.1:19643/")).ok) break; } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext();
  page = await context.newPage();
  await page.goto("http://127.0.0.1:19643/");
  await page.locator('input[type="password"]').fill("test-password");
  await page.getByRole("button", { name: "Login", exact: true }).click();
  const defaults = await page.evaluate(async () => (await (await fetch("/api/sessions")).json()).new_session_defaults);
  assert(defaults.backends.pi.provider_models.completions?.includes("second"), "Fixture models did not load");
  assert(defaults.backends.pi.provider_models.openai?.length > 0, "Built-in model catalog did not load through the configured SDK");
  await page.locator("#newBtn").click();
  await page.locator("#newSessionModelInput").click();
  await page.locator("#newSessionModelInput").fill("completions/");
  await page.locator("#newSessionModelMenu").getByRole("option", { name: "second · completions", exact: true }).click();
  await page.locator("#newSessionResumeBtn").click();
  await page.getByRole("searchbox", { name: "Search conversation history" }).fill("Federated");
  const entry = page.locator("#newSessionResumeMenu").getByRole("button", { name: /Federated conversation/ });
  await entry.waitFor();
  assert.equal(await entry.count(), 1, "Source and mirror appeared as duplicate conversations");
  assert((await entry.innerText()).includes("NUC test"));
  await page.screenshot({ path: "/tmp/federated-history-menu.png" });
  await entry.click();
  assert.equal(await page.locator("#newSessionCwdInput").inputValue(), cwd);
  await page.locator("#newSessionStartBtn").click();
  await page.locator("#chatInner").getByText("original-remote-question", { exact: true }).waitFor({ timeout: 30000 });
  await page.locator("#msg").fill("continue-on-original-machine");
  await page.locator("#sendBtn").click();
  if (await page.locator("#sendChoiceNowBtn").isVisible()) await page.locator("#sendChoiceNowBtn").click();
  await page.locator("#chatInner").getByText("remote-continuation-ok", { exact: true }).waitFor({ timeout: 45000 });
  assert(providerRequest.messages.some(row => JSON.stringify(row).includes("original-remote-question")));
  assert(providerRequest.messages.some(row => row.role === "tool" && JSON.stringify(row).includes("original-tool-result")));
  assert.equal(providerRequest.model, "second");
  assert.equal(readFileSync(folder + "/native.jsonl", "utf8"), transcript, "Gateway changed its history mirror while continuing on the worker");
  assert.equal(context.pages().length, 1);
  const before = await page.evaluate(async () => (await (await fetch("/api/sessions")).json()).sessions);
  assert.equal(before.length, 1);
  assert(before[0].session_id.startsWith("nuc~"));
  assert.equal(before[0].thread_id, sourceId);
  await page.locator("#newBtn").click();
  await page.locator("#newSessionResumeBtn").click();
  await page.locator("#newSessionResumeMenu").getByRole("button", { name: /Federated conversation/ }).click();
  const launch = page.waitForResponse(response => response.url().endsWith("/api/sessions") && response.request().method() === "POST");
  await page.locator("#newSessionStartBtn").click();
  assert.equal((await (await launch).json()).existing_session_id, before[0].session_id);
  await page.locator("#newSessionViewer").waitFor({ state: "hidden" });
  const after = await page.evaluate(async () => (await (await fetch("/api/sessions")).json()).sessions);
  assert.equal(after.length, 1);
  assert.equal(after[0].broker_pid, before[0].broker_pid);
  await page.screenshot({ path: "/tmp/federated-continuation.png" });

  await page.locator("#newBtn").click();
  await page.locator("#newSessionResumeBtn").click();
  await page.locator("#newSessionResumeMenu").getByRole("button", { name: /Federated conversation/ }).click();
  let unexpectedCreate = false;
  page.on("request", request => { if (request.method() === "POST" && request.url().endsWith("/api/sessions")) unexpectedCreate = true; });
  writeFileSync(config + "/peers.json", JSON.stringify({ peers: [{ ...peer, url: "http://127.0.0.1:1" }] }));
  for (let n = 0; n < 30; n++) {
    const devices = await page.evaluate(async () => (await (await fetch("/api/sessions")).json()).devices);
    if (devices[0]?.online === false) break;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  await page.waitForFunction(() => document.querySelector("#newSessionStartBtn")?.textContent === "Unavailable");
  assert((await page.locator("#newSessionResumeBtn").innerText()).includes("Federated conversation"));
  await page.locator("#newSessionStartBtn").click();
  assert.equal(unexpectedCreate, false, "Offline selection silently started a fresh conversation");
  await page.locator("#newSessionResumeBtn").click();
  const saved = page.locator("#newSessionResumeMenu").getByRole("link", { name: /Read-only history.*Federated conversation/ });
  await saved.waitFor();
  assert(!(await saved.getAttribute("href")).includes("nuc~"));
  assert((await saved.innerText()).includes("offline"));
  console.log(JSON.stringify({ remoteResume: true, originalContextAndToolsPreserved: true, selectedModelApplied: true,
    existingBrokerReused: true, mirrorNotModified: true, singleBrowserPage: true, offlineSavedCopy: true, offlineSelectionPreserved: true }));
} catch (error) {
  if (page) { await page.screenshot({ path: "/tmp/federation-error.png" }); console.log((await page.locator("body").innerText()).slice(-5000)); }
  throw error;
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  provider.closeAllConnections();
  provider.close();
}
