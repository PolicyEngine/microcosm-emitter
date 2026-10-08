import { mkdtempSync, writeFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const files = readdirSync("reader-dist").filter((name) =>
  name.endsWith(".tgz"),
);
if (files.length !== 1) throw new Error("Expected exactly one reader tarball.");
const workspace = mkdtempSync(join(tmpdir(), "provider-reader-"));
function run(args: string[]) {
  const result = Bun.spawnSync(args, {
    cwd: workspace,
    stdout: "inherit",
    stderr: "inherit",
  });
  if (result.exitCode !== 0) throw new Error("Installed reader check failed.");
}
try {
  writeFileSync(
    join(workspace, "package.json"),
    JSON.stringify({
      private: true,
      type: "module",
      dependencies: {
        "@policyengine/microcosm-provider-orrery": resolve(
          "reader-dist",
          files[0],
        ),
        "@axiom-foundation/orrery": "0.6.1",
      },
    }),
  );
  run(["bun", "install", "--ignore-scripts"]);
  writeFileSync(
    join(workspace, "check.ts"),
    `
import { loadDocument } from "@policyengine/microcosm-provider-orrery/browser";
import { createPublicationReader } from "@policyengine/microcosm-provider-orrery/server";
const reader = createPublicationReader({read: async () => null});
if (await reader.read("missing") !== null) throw new Error("Expected missing publication.");
try {
  await loadDocument("missing", new AbortController().signal, async () => new Response("", {status: 404}));
  throw new Error("Missing publication was accepted.");
} catch (error) {
  if (!(error instanceof Error) || !error.message.includes("not found")) throw error;
}
`,
  );
  run(["bun", "check.ts"]);
  writeFileSync(
    join(workspace, "browser.ts"),
    'export { loadDocument } from "@policyengine/microcosm-provider-orrery/browser";',
  );
  run([
    "bun",
    "build",
    "browser.ts",
    "--target",
    "browser",
    "--outfile",
    "browser.js",
  ]);
} finally {
  rmSync(workspace, { recursive: true, force: true });
}
