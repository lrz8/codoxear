import { chromium } from "/usr/local/lib/node_modules/playwright/index.mjs";
import { mkdirSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { spawn } from "node:child_process";
import assert from "node:assert/strict";
import { createServer } from "node:http";

// Run only inside an isolated Docker container with Pi, Chromium, Playwright,
// Python dependencies and this checkout on PYTHONPATH. Uses a local mock model.
if (!existsSync("/.dockerenv")) throw new Error("Pi history verification requires Docker isolation");

const agent = "/root/.pi/agent";
mkdirSync(agent + "/sessions/--data-old-project--", { recursive: true });
mkdirSync(agent + "/sessions/Mac/--remote--", { recursive: true });
mkdirSync("/data/old-project", { recursive: true });
mkdirSync("/data/new-project", { recursive: true });
writeFileSync(agent + "/auth.json", "{}");
writeFileSync(agent + "/settings.json", JSON.stringify({ defaultProvider: "completions", defaultModel: "shared", defaultThinkingLevel: "off" }));
const model = { id: "shared", name: "Shared", reasoning: false, input: ["text"], contextWindow: 100000, maxTokens: 1000, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
writeFileSync(agent + "/models.json", JSON.stringify({ providers: {
  completions: { api: "openai-completions", baseUrl: "http://127.0.0.1:19999/v1", apiKey: "test-key", models: [model, { ...model, id: "second" }] },
  responses: { api: "openai-responses", baseUrl: "http://127.0.0.1:19999/v1", apiKey: "test-key", models: [model, { ...model, id: "second" }] },
} }));
const transcript = (id, cwd, text) => [
  { type: "session", version: 3, id, cwd, timestamp: "2026-10-14T00:00:00Z" },
  { type: "model_change", id: "model", provider: "completions", modelId: "shared" },
  { type: "message", id: "message", parentId: "model", message: { role: "user", content: [{ type: "text", text }], timestamp: 1 } },
  { type: "message", id: "reply", parentId: "message", message: { role: "assistant", content: [{ type: "text", text: "original-reply" }], api: "openai-completions", provider: "completions", model: "shared", stopReason: "stop", usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, timestamp: 2 } },
].map(row => JSON.stringify(row)).join("\n") + "\n";
writeFileSync(agent + "/sessions/--data-old-project--/native.jsonl", transcript("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "/data/old-project", "native-history-marker"));
writeFileSync(agent + "/sessions/Mac/--remote--/mirror.jsonl", transcript("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "/unavailable/mac-project", "mirror-history-marker"));
let providerRequest;
const providerServer = createServer(async (req, res) => {
  let body = "";
  for await (const chunk of req) body += chunk;
  providerRequest = JSON.parse(body);
  res.writeHead(200, { "content-type": "text/event-stream" });
  res.write('data: ' + JSON.stringify({ id: "test-response", object: "chat.completion.chunk", choices: [{ index: 0, delta: { role: "assistant", content: "continued-history-ok" }, finish_reason: null }] }) + '\n\n');
  res.write('data: ' + JSON.stringify({ id: "test-response", object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } }) + '\n\n');
  res.end('data: [DONE]\n\n');
});
await new Promise(resolve => providerServer.listen(19999, "127.0.0.1", resolve));
const server = spawn("python3", ["-m", "codoxear.server"], { env: { ...process.env, CODEX_WEB_PASSWORD: "test-password", CODEX_WEB_HOST: "127.0.0.1", CODEX_WEB_PORT: "19643", CODEX_WEB_DEFAULT_AGENT_BACKEND: "pi" }, stdio: ["ignore", "ignore", "inherit"] });
let browser;
let page;
try {
  for (let n = 0; n < 40; n++) {
    try { const response = await fetch("http://127.0.0.1:19643/"); if (response.ok) break; } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  browser = await chromium.launch({ executablePath: "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext();
  page = await context.newPage();
  page.on("response", async response => { if (response.url().includes("/send")) console.log("SEND", response.status(), await response.text()); });
  await page.goto("http://127.0.0.1:19643/");
  await page.locator('input[type="password"]').fill("test-password");
  await page.getByRole("button", { name: "Login", exact: true }).click();
  const defaults = await page.evaluate(async () => (await (await fetch("/api/sessions")).json()).new_session_defaults);
  writeFileSync("/tmp/api-defaults.json", JSON.stringify(defaults));
  await page.locator("#newBtn").click();
  await page.locator("#newSessionCwdInput").fill("/data/new-project");
  await page.locator("#newSessionModelInput").click();
  await page.locator("#newSessionModelInput").fill("completions/");
  await page.locator("#newSessionModelMenu").getByRole("option", { name: "second · completions", exact: true }).click();
  assert.equal(await page.locator("#newSessionModelInput").inputValue(), "second · completions");
  await page.locator("#newSessionResumeBtn").click();
  await page.getByRole("button", { name: /native-history-marker/ }).waitFor();
  assert.equal(await page.getByRole("link", { name: /native-history-marker/ }).count(), 0);
  assert.equal(await page.locator("#newSessionResumeMenu").getByText(/native-history-marker/).count(), 1);
  await page.getByRole("link", { name: /Read-only history.*mirror-history-marker/ }).waitFor();
  const href = await page.getByRole("link", { name: /Read-only history.*mirror-history-marker/ }).getAttribute("href");
  // Use the same authenticated context when viewing the read-only history.
  const history = await page.context().newPage();
  await history.goto(new URL(href, "http://127.0.0.1:19643").href);
  await history.getByText("mirror-history-marker", { exact: true }).waitFor();
  await history.close();
  await page.getByRole("button", { name: /native-history-marker/ }).click();
  assert.equal(await page.locator("#newSessionCwdInput").inputValue(), "/data/old-project");
  await page.screenshot({ path: "/tmp/models-history-verification.png" });
  const started = page.waitForResponse(response => response.url().endsWith("/api/sessions") && response.request().method() === "POST");
  await page.locator("#newSessionStartBtn").click();
  const launch = await started;
  assert(launch.ok(), JSON.stringify(await launch.json()));
  let launched;
  for (let attempt = 0; attempt < 40; attempt++) {
    const rows = await page.evaluate(async () => (await (await fetch("/api/sessions")).json()).sessions);
    launched = rows.find(row => row.agent_backend === "pi" && row.model === "second");
    if (launched) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  assert(launched, "Selected model was not applied to the resumed Pi session");
  await page.locator("#chatInner").getByText("native-history-marker", { exact: true }).waitFor({ timeout: 30000 });
  await page.locator("#msg").fill("continue-this-history");
  await page.locator("#sendBtn").click();
  if (await page.locator("#sendChoiceNowBtn").isVisible()) await page.locator("#sendChoiceNowBtn").click();
  await page.locator("#chatInner").getByText("continued-history-ok", { exact: true }).waitFor({ timeout: 45000 });
  assert(providerRequest.messages.some(message => JSON.stringify(message.content).includes("native-history-marker")), "Original history did not reach the model");
  assert(providerRequest.messages.some(message => JSON.stringify(message.content).includes("continue-this-history")), "New message did not reach the model");
  assert.equal(providerRequest.model, "second");
  assert.equal(context.pages().length, 1, "Continuing a conversation opened another page");
  assert.equal(new URL(page.url()).pathname, "/");
  await page.screenshot({ path: "/tmp/history-continuation.png" });
  const output = JSON.parse(readFileSync("/tmp/api-defaults.json", "utf8"));
  const pi = output.backends?.pi || output;
  assert.deepEqual(pi.provider_models.responses, ["shared", "second"]);
  assert(pi.provider_models.completions.includes("shared"));
  assert(pi.provider_models.openai?.length > 0, "Built-in Pi catalog missing");
  console.log(JSON.stringify({ modelSelection: "completions/second", modelLaunch: true, historyContextPreserved: true, sentAndReceivedInSamePage: true, nativeResumeCwd: "/data/old-project", mirrorView: true, builtinModelCount: pi.provider_models.openai.length }));
} catch (error) {
  if (page) { await page.screenshot({ path: "/tmp/history-error.png" }); console.log("PAGE", (await page.locator("body").innerText()).slice(-7000)); }
  console.log("PROVIDER", JSON.stringify(providerRequest));
  throw error;
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  providerServer.closeAllConnections();
  providerServer.close();
}
