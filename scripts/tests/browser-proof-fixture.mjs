// Build existing real-App fixtures for the bounded Python browser proof.
// Run: node scripts/tests/browser-proof-fixture.mjs /tmp/llmrouter-browser-proof-<id>
// This tool does not start a server or browser and does not read a session.
import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import {
  lstat,
  mkdir,
  readFile,
  realpath,
  rm,
  writeFile,
} from "node:fs/promises";
import { createRequire } from "node:module";
import { basename, dirname, resolve } from "node:path";
import process from "node:process";

const root = resolve(import.meta.dirname, "../..");
const fixtureSources = {
  shell: "apps/admin/test/fixtures/shell-browser.tsx",
  creation: "apps/admin/test/fixtures/service-creation-browser.tsx",
  logs: "apps/admin/test/fixtures/logs-browser.tsx",
};
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");

async function buildFixtures() {
  const output = process.argv[2];
  if (
    process.argv.length !== 3 ||
    typeof output !== "string" ||
    dirname(output) !== "/tmp" ||
    resolve(output) !== output ||
    !/^llmrouter-browser-proof-[A-Za-z0-9_-]+$/.test(basename(output)) ||
    (await realpath("/tmp")) !== "/tmp"
  ) {
    throw Error("Use one fresh /tmp/llmrouter-browser-proof-<id> directory.");
  }
  // mkdir is exclusive. Existing paths, including symlinks, cannot be reused.
  await mkdir(output, { mode: 0o700 });
  const created = await lstat(output);
  if (!created.isDirectory() || created.isSymbolicLink()) {
    throw Error("The fixture output must be a new directory.");
  }
  try {
    const requireTools = createRequire(
      resolve(
        process.env.OPENDLE_UI_PATH ?? resolve(root, "../opendle-ui"),
        "package.json",
      ),
    );
    const { build } = requireTools("esbuild");
    const requireAdmin = createRequire(
      resolve(root, "apps/admin/package.json"),
    );
    const shared = dirname(requireAdmin.resolve("@opendle/ui/package.json"));
    const sharedEntry = resolve(shared, "dist/index.js");
    const sharedTokens = resolve(shared, "styles/tokens.css");
    const routerStyles = resolve(root, "apps/admin/src/styles.css");
    const [entryBytes, tokenBytes, styleBytes] = await Promise.all([
      readFile(sharedEntry),
      readFile(sharedTokens),
      readFile(routerStyles),
    ]);
    const manifest = {
      version: 1,
      origin: "http://127.0.0.1:5174",
      shared: {
        entry: "dist/index.js",
        entry_sha256: digest(entryBytes),
        tokens: "styles/tokens.css",
        tokens_sha256: digest(tokenBytes),
      },
      router_styles_sha256: digest(styleBytes),
      assets: {},
    };
    const writeAsset = async (name, bytes, contentType, source) => {
      await writeFile(resolve(output, name), bytes, {
        flag: "wx",
        mode: 0o600,
      });
      manifest.assets["/" + name] = {
        file: name,
        content_type: contentType,
        sha256: digest(bytes),
        ...(source
          ? {
              source,
              source_sha256: digest(await readFile(resolve(root, source))),
            }
          : {}),
      };
    };
    await writeAsset(
      "fixture.css",
      Buffer.concat([tokenBytes, styleBytes]),
      "text/css",
    );
    for (const [name, source] of Object.entries(fixtureSources)) {
      const bundle = await build({
        entryPoints: [resolve(root, source)],
        bundle: true,
        format: "iife",
        platform: "browser",
        write: false,
        logLevel: "silent",
        jsx: "automatic",
        alias: {
          "@opendle/ui": sharedEntry,
          react: resolve(root, "node_modules/react"),
          "react-dom": resolve(root, "node_modules/react-dom"),
        },
      });
      const bytes = bundle.outputFiles[0]?.contents;
      if (!bytes?.length) throw Error("The fixture bundle is empty.");
      await writeAsset(name + ".js", bytes, "text/javascript", source);
    }
    await writeFile(
      resolve(output, "manifest.json"),
      JSON.stringify(manifest, null, 2) + "\n",
      {
        flag: "wx",
        mode: 0o600,
      },
    );
    process.stdout.write("Browser proof fixtures built.\n");
  } catch {
    await rm(output, { recursive: true, force: true });
    // Do not print dependency diagnostics, environment values, or source data.
    throw Error("Browser proof fixture build failed.");
  }
}

try {
  await buildFixtures();
} catch {
  process.stderr.write(
    "Browser proof fixture build failed. Use a fresh /tmp/llmrouter-browser-proof-<id> directory and installed dependencies.\n",
  );
  process.exitCode = 1;
}
