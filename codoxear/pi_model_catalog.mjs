// Read the same local model registry as Pi without starting an agent session.
import { realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const [binary, modelsPath, authPath, modelsStorePath] = process.argv.slice(2);
let directory = dirname(realpathSync(binary));
while (!directory.endsWith("/dist") && directory !== dirname(directory)) directory = dirname(directory);
// Managed launchers may be shell scripts outside the SDK's dist directory.
if (process.env.CODOXEAR_PI_PACKAGE_ROOT) directory = join(process.env.CODOXEAR_PI_PACKAGE_ROOT, "dist");
const { ModelRuntime } = await import(pathToFileURL(join(directory, "core/model-runtime.js")));
const { ReadOnlyAuthStorage } = await import(pathToFileURL(join(directory, "core/auth-storage.js")));
const runtime = await ModelRuntime.create({
  credentials: new ReadOnlyAuthStorage(authPath), modelsPath, modelsStorePath,
  allowModelNetwork: false,
});
const models = runtime.getModels().map(model => ({
  provider: model.provider, id: model.id, reasoning: model.reasoning,
}));
const providerAuth = Object.fromEntries([...new Set(models.map(model => model.provider))]
  .map(provider => [provider, runtime.hasConfiguredAuth(provider)]));
process.stdout.write(JSON.stringify({ models, provider_auth: providerAuth }));
