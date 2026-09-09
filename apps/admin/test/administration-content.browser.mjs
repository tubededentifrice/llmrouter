// Run: node apps/admin/test/administration-content.browser.mjs
/* global window, document, innerWidth, Blob, URL */
// Real App, installed shared UI, existing controlled client fixture, loopback only.
import { strict as assert } from "node:assert";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { after, before, it } from "node:test";
const root = resolve(import.meta.dirname, "../../..");
const requireShared = createRequire(
  resolve(root, "../opendle-ui/package.json"),
);
const { chromium, expect } = requireShared("@playwright/test");
const { build } = requireShared("esbuild");
const { default: AxeBuilder } = requireShared("@axe-core/playwright");
const shared = dirname(
  createRequire(import.meta.url).resolve("@opendle/ui/package.json"),
);
const evidence = resolve(
  process.env.LLMROUTER_CONTENT_EVIDENCE ?? "/tmp/llmr-8za2-content-browser",
);
const inventory = JSON.parse(
  await readFile(
    resolve(import.meta.dirname, "fixtures/administration-content.json"),
    "utf8",
  ),
);
const results = [];
let browser, script, css;
const pageOf = (items) => ({ items, page: { has_more: false } });
const date = "2026-09-09T00:00:00Z";
const summary = {
  id: "content-log",
  logical_call_id: "content-call",
  call_actor: "administrator",
  administrator_subject: "fixture-administrator",
  provider_model_api_name: "route",
  kind: "model",
  outcome: "succeeded",
  started_at: date,
  tags: [],
};
const detail = {
  summary,
  request_json: '{"prompt":"Synthetic content"}',
  response_json: '{"content":"Synthetic response"}',
  attempts: [],
  media: [
    {
      id: "content-media",
      role: "input",
      media_type: "image/png",
      size_bytes: 4,
    },
  ],
};
const assignment = {
  api_name: "assignment",
  display_name: "Fixture assignment",
  definition_kind: "direct_chain",
  defined_by_service_api_name: "root",
  direct_chain: [{ provider_model_api_name: "route" }],
  effective_chain: [{ provider_model_api_name: "route" }],
  observed_requirements: ["streaming"],
};
before(async () => {
  const bundle = await build({
    entryPoints: [resolve(root, "apps/admin/test/fixtures/shell-browser.tsx")],
    bundle: true,
    format: "iife",
    platform: "browser",
    write: false,
    logLevel: "silent",
    jsx: "automatic",
    alias: {
      "@opendle/ui": resolve(shared, "dist/index.js"),
      react: resolve(root, "node_modules/react"),
      "react-dom": resolve(root, "node_modules/react-dom"),
    },
  });
  script = bundle.outputFiles[0].text;
  css =
    (await readFile(resolve(shared, "styles/tokens.css"), "utf8")) +
    (await readFile(resolve(root, "apps/admin/src/styles.css"), "utf8"));
  await mkdir(evidence, { recursive: true });
  browser = await chromium.launch({
    headless: true,
    ...(existsSync("/usr/bin/google-chrome")
      ? { executablePath: "/usr/bin/google-chrome" }
      : {}),
  });
});
after(async () => {
  await browser?.close();
  await writeFile(
    resolve(evidence, "results.json"),
    JSON.stringify(results, null, 2) + "\n",
  );
});
const button = (page, name) => page.getByRole("button", { name, exact: true });
async function patch(page, values) {
  await page.evaluate(
    (values) => Object.assign(window.shellFixture.values, values),
    values,
  );
}
async function mode(page, value) {
  await page.evaluate(
    (value) => Object.assign(window.shellFixture, value),
    value,
  );
}
async function finish(page, name, fail = false) {
  await page.evaluate(
    ({ name, fail }) => window.shellFixture.finish(name, fail),
    { name, fail },
  );
}
async function settle(page) {
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
}
async function open(width, scene) {
  const context = await browser.newContext({
    viewport: { width, height: width === 390 ? 844 : 1000 },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(6000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    assert.equal(url.origin, "http://127.0.0.1:5174");
    if (url.pathname === "/content.js")
      return route.fulfill({ contentType: "text/javascript", body: script });
    if (url.pathname === "/content.css")
      return route.fulfill({ contentType: "text/css", body: css });
    return route.fulfill({
      contentType: "text/html",
      body: '<!doctype html><html lang="en"><head><title>Administration content</title><link rel="stylesheet" href="/content.css"></head><body><div id="root"></div><script>window.shellBoot={hold:["session"],fail:[],signedOut:false}</script><script src="/content.js"></script></body></html>',
    });
  });
  await page.goto("http://127.0.0.1:5174" + scene.path);
  await expect
    .poll(() => page.evaluate(() => window.shellFixture?.pending.length))
    .toBe(1);
  await patch(page, {
    assignments: pageOf([assignment]),
    requestLogsPage: pageOf([summary]),
    requestLog: detail,
    ...scene.values,
  });
  await mode(page, { hold: scene.hold ?? [], fail: scene.fail ?? [] });
  await finish(page, "session");
  await expect(page.locator("main h1")).toHaveCount(1);
  if (!scene.hold?.length) await settle(page);
  return { context, page, errors };
}
async function proof(page, scene, width) {
  const route = inventory.routes.find((route) => route.path === scene.route);
  assert.ok(route, scene.route);
  for (const item of inventory.routes
    .flatMap((route) => route.items)
    .filter((item) => item.expected === "absent" && !item.absenceReason))
    await expect(page.getByText(item.text, { exact: true })).toHaveCount(0);
  for (const item of route.items) {
    if (item.expected === "absent")
      await expect(page.getByText(item.text, { exact: true })).toHaveCount(0);
    else if (item.scenes.includes(scene.id)) {
      await expect(
        page
          .getByText(item.text, { exact: true })
          .filter({ visible: true })
          .first(),
      ).toBeVisible();
    }
  }
  for (const name of scene.names ?? [])
    await expect(
      page.getByRole(name.role, { name: name.name, exact: true }).first(),
    ).toBeVisible();
  for (const live of scene.live ?? [])
    await expect(
      page
        .locator('[role="status"], [role="alert"], [aria-live]')
        .filter({ hasText: live })
        .first(),
    ).toHaveCount(1);
  const axe = await new AxeBuilder({ page }).analyze();
  assert.deepEqual(axe.violations, [], `${width} ${scene.id} Axe`);
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  if (
    scene.id.endsWith("normal") &&
    ["/overview", "/operations"].includes(scene.route)
  )
    await expect(page.locator(".health-list small")).toHaveCount(0);
  const screenshot = `${width}-${scene.id}.png`;
  await page.screenshot({
    path: resolve(evidence, screenshot),
    fullPage: true,
  });
  results.push({
    width,
    scene: scene.id,
    route: scene.route,
    screenshot,
    axeViolations: 0,
    checked: route.items
      .filter(
        (item) => item.expected === "absent" || item.scenes.includes(scene.id),
      )
      .map((item) => item.id),
  });
}
const scenes = [];
function scene(id, route, options = {}) {
  scenes.push({
    id,
    route,
    path: route.replace("{serviceApiName}", "root"),
    ...options,
  });
}
for (const [route, heading] of [
  ["/overview", "Overview"],
  ["/services", "Services"],
  ["/services/{serviceApiName}", "Root service"],
  ["/configuration", "LLM configuration"],
  ["/logs", "Logs"],
  ["/statistics", "Usage and cost statistics"],
  ["/operations", "Activity & health"],
]) {
  scene(
    route === "/services/{serviceApiName}"
      ? "details-normal"
      : route.slice(1) + "-normal",
    route,
    { names: [{ role: "heading", name: heading }] },
  );
}
scene("overview-health", "/overview", {
  values: {
    health: {
      status: "degraded",
      checked_at: date,
      components: [
        {
          name: "storage",
          status: "degraded",
          message: "Restore storage access before you retry the request.",
        },
      ],
    },
  },
});
scene("overview-loading", "/overview", {
  hold: ["services", "providers", "providerModels", "health"],
  live: ["Loading Overview."],
});
scene("overview-failure", "/overview", {
  fail: ["services", "providers", "providerModels", "health"],
  live: ["Overview is unavailable."],
});
scene("overview-stale", "/overview", {
  action: async (page) => {
    await mode(page, { fail: ["health"] });
    await button(page, "Refresh overview").click();
    await settle(page);
  },
  live: ["Overview is partial or stale. Retry the failed summaries."],
});
scene("services-compact", "/services", {
  path: "/services?service=root",
  names: [{ role: "button", name: "Open service details" }],
});
scene("services-create", "/services", {
  path: "/services?service=root",
  action: async (page) => {
    if (await page.getByRole("dialog").count())
      await page.keyboard.press("Escape");
    await button(page, "New service under Root service, API name root").click();
  },
  names: [
    { role: "textbox", name: "Display name" },
    { role: "textbox", name: "API name" },
  ],
});
scene("services-create-error", "/services", {
  path: "/services?service=root",
  action: async (page) => {
    if (await page.getByRole("dialog").count())
      await page.keyboard.press("Escape");
    await button(page, "New service under Root service, API name root").click();
    await button(page, "Create service").click();
  },
  live: ["The service was not created."],
});
scene("services-discard", "/services", {
  path: "/services?service=root",
  action: async (page) => {
    if (await page.getByRole("dialog").count())
      await page.keyboard.press("Escape");
    await button(page, "New service under Root service, API name root").click();
    await page
      .getByRole("textbox", { name: "Display name", exact: true })
      .fill("Draft");
    const width = page.viewportSize().width;
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('[data-service-api-name="child"]').click();
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  },
  names: [{ role: "dialog", name: "Discard service values?" }],
});
scene("services-loading", "/services", {
  hold: ["services"],
  live: ["Loading Services."],
});
scene("services-unavailable", "/services", {
  fail: ["services"],
  live: ["Services are unavailable."],
});
scene("details-child", "/services/{serviceApiName}", {
  path: "/services/child",
});
scene("details-delete", "/services/{serviceApiName}", {
  path: "/services/child",
  action: async (page) => button(page, "Delete service").click(),
  names: [{ role: "textbox", name: "Enter the impact statement to continue" }],
});
scene("details-workspace", "/services/{serviceApiName}", {
  action: async (page) => button(page, "Create workspace").click(),
  names: [
    { role: "textbox", name: "Workspace API name" },
    { role: "textbox", name: "Workspace display name" },
  ],
});
scene("details-workspace-delete", "/services/{serviceApiName}", {
  values: {
    workspaces: pageOf([
      {
        api_name: "workspace",
        display_name: "Fixture workspace",
        created_at: date,
      },
    ]),
  },
  action: async (page) =>
    page
      .getByRole("button", { name: "Delete Fixture workspace", exact: true })
      .click(),
});
scene("details-revoke", "/services/{serviceApiName}", {
  values: {
    keys: pageOf([
      { id: "key", name: "Fixture key", created_at: date, last_used_at: null },
    ]),
  },
  action: async (page) => button(page, "Revoke Fixture key").click(),
});
async function createKey(page) {
  await button(page, "Create key").click();
  await page
    .getByRole("textbox", { name: "Key name", exact: true })
    .fill("Fixture key");
  await button(page, "Create New service API key").click();
}
scene("details-key-pending", "/services/{serviceApiName}", {
  action: async (page) => {
    await mode(page, { hold: ["createKey"] });
    await createKey(page);
  },
});
scene("details-key-secret", "/services/{serviceApiName}", {
  action: createKey,
  names: [{ role: "button", name: "Clear secret" }],
  live: ["The service API key was created."],
});
scene("details-loading", "/services/{serviceApiName}", {
  hold: ["service"],
  live: ["Loading service details."],
});
scene("details-absent", "/services/{serviceApiName}", {
  path: "/services/missing",
});
scene("details-access-error", "/services/{serviceApiName}", {
  fail: ["workspaces", "keys"],
});
scene("configuration-provider", "/configuration", {
  action: async (page) => {
    await button(page, "Add provider").click();
    await page
      .getByText("Advanced settings and review", { exact: true })
      .click();
  },
  names: [{ role: "textbox", name: "Credential API name" }],
});
scene("configuration-provider-local", "/configuration", {
  action: async (page) => {
    await button(page, "Add provider").click();
    await page
      .getByRole("combobox", { name: "Adapter", exact: true })
      .selectOption("ollama");
    await page
      .getByText("Advanced settings and review", { exact: true })
      .click();
  },
});
scene("configuration-model", "/configuration", {
  action: async (page) => button(page, "Add canonical model").click(),
  names: [
    { role: "textbox", name: "API name" },
    { role: "textbox", name: "Input modalities" },
  ],
});
scene("configuration-assignment", "/configuration", {
  path: "/configuration?service=root",
  action: async (page) => button(page, "Add assignment").click(),
  names: [{ role: "textbox", name: "Assignment API name" }],
});
scene("configuration-assignment-required", "/configuration", {
  action: async (page) => expect(button(page, "Add assignment")).toBeDisabled(),
});
scene("configuration-loading", "/configuration", {
  hold: ["providers", "models", "providerModels", "credentials"],
  live: ["Loading configuration"],
});
scene("configuration-empty", "/configuration", {
  values: {
    providers: pageOf([]),
    models: pageOf([]),
    providerModels: pageOf([]),
  },
});
scene("configuration-failure", "/configuration", {
  fail: ["providers", "models", "providerModels", "credentials"],
});
scene("logs-empty", "/logs", { values: { requestLogsPage: pageOf([]) } });
scene("logs-loading", "/logs", {
  hold: ["requestLogsPage"],
  live: ["Loading Logs."],
});
scene("logs-failure", "/logs", {
  fail: ["requestLogsPage"],
  live: ["Logs are unavailable."],
});
scene("logs-retention-failure", "/logs", { fail: ["retention"] });
scene("logs-detail", "/logs", {
  action: async (page) =>
    page
      .getByRole("radio", {
        name: "Inspect Logs details for request content-log",
        exact: true,
      })
      .check(),
  live: ["Logs details loaded."],
});
scene("logs-detail-loading", "/logs", {
  action: async (page) => {
    await mode(page, { hold: ["requestLog"] });
    await page
      .getByRole("radio", {
        name: "Inspect Logs details for request content-log",
        exact: true,
      })
      .check();
  },
  live: ["Loading Logs details."],
});
scene("logs-detail-failure", "/logs", {
  fail: ["requestLog"],
  action: async (page) =>
    page
      .getByRole("radio", {
        name: "Inspect Logs details for request content-log",
        exact: true,
      })
      .check(),
  live: ["Logs details are unavailable."],
});
scene("logs-media-empty", "/logs", {
  values: { requestLog: { ...detail, media: [], response_json: null } },
  action: async (page) =>
    page
      .getByRole("radio", {
        name: "Inspect Logs details for request content-log",
        exact: true,
      })
      .check(),
});
scene("logs-media-ready", "/logs", {
  action: async (page) => {
    await page.evaluate(() => {
      window.shellFixture.values.requestLogMedia = new Blob(["test"], {
        type: "image/png",
      });
    });
    await page
      .getByRole("radio", {
        name: "Inspect Logs details for request content-log",
        exact: true,
      })
      .check();
    await button(page, "Prepare retained media download").click();
  },
  names: [{ role: "link", name: "Download retained media" }],
});
scene("statistics-loading", "/statistics", {
  action: async (page) => {
    await mode(page, { hold: ["statistics"] });
    await button(page, "Run statistics").click();
  },
});
scene("statistics-failure", "/statistics", {
  fail: ["statistics"],
  action: async (page) => button(page, "Run statistics").click(),
});
scene("statistics-empty", "/statistics", {
  action: async (page) => button(page, "Run statistics").click(),
});
scene("operations-health", "/operations", {
  values: {
    health: {
      status: "degraded",
      checked_at: date,
      components: [
        {
          name: "storage",
          status: "degraded",
          message: "Restore storage access before you retry the request.",
        },
      ],
    },
  },
});
scene("operations-loading", "/operations", {
  hold: ["health", "retention", "providerModels", "activityPage"],
});
scene("operations-failure", "/operations", {
  fail: ["health", "retention", "providerModels", "activityPage"],
});
scene("operations-empty", "/operations", {
  values: { providerModels: pageOf([]) },
});

async function node(page, id) {
  await page.locator('[data-node-id="' + id + '"]').click();
}
async function mediaSetup(page) {
  await page.evaluate(() => {
    const f = window.shellFixture;
    for (const key of ["models", "providerModels"])
      f.values[key] = {
        ...f.values[key],
        items: f.values[key].items.map((item) => ({
          ...item,
          cooldown: null,
          input_modalities: ["text", "image"],
          output_modalities: ["text", "image", "embedding", "video", "audio"],
        })),
      };
  });
  await button(page, "Refresh configuration").click();
  await settle(page);
  await node(page, "mapping:route");
  await button(page, "Play exact route").click();
}
scene("configuration-provider-no-credential", "/configuration", {
  action: async (page) => {
    await button(page, "Add provider").click();
    await page
      .getByRole("combobox", { name: "Adapter", exact: true })
      .selectOption("fake");
  },
});
scene("configuration-mapping", "/configuration", {
  action: async (page) => {
    await node(page, "mapping:route");
    await page
      .getByText("Capabilities, reasoning, and price", { exact: true })
      .click();
  },
});
scene("configuration-observed", "/configuration", {
  path: "/configuration?service=root",
  action: async (page) => node(page, "assignment:assignment"),
});
scene("configuration-import", "/configuration", {
  action: async (page) => {
    await page.evaluate(() => {
      const f = window.shellFixture;
      f.values.previewOpenRouterModel = {
        source_model_id: "fixture/model",
        model: { ...f.values.models.items[0], api_name: "new-model" },
        reasoning: { supported: false },
        supported_constraints: [],
        issues: [],
        conflicts: [],
        provider_options: [],
        can_confirm: false,
      };
    });
    await button(page, "Add canonical model").click();
    await page
      .getByRole("textbox", {
        name: "Exact model ID or supported OpenRouter URL",
        exact: true,
      })
      .fill("fixture/model");
    await button(page, "Preview OpenRouter model").click();
  },
});
scene("configuration-context-discard", "/configuration", {
  path: "/configuration?service=root",
  action: async (page) => {
    const width = page.viewportSize().width;
    await page.setViewportSize({ width: 1440, height: 1000 });
    await button(page, "Add assignment").click();
    await page
      .getByRole("textbox", { name: "Assignment API name", exact: true })
      .fill("draft");
    await page
      .getByRole("combobox", { name: "Service context", exact: true })
      .selectOption("child");
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
  },
});
scene("configuration-playground", "/configuration", {
  action: mediaSetup,
  names: [
    {
      role: "textbox",
      name: "Prompt Enter the prompt for the model operation.",
    },
    { role: "button", name: "Run operation" },
  ],
});
scene("configuration-media-uncertain", "/configuration", {
  action: async (page) => {
    await mediaSetup(page);
    await mode(page, { fail: ["playgroundCreateMedia"] });
    await page
      .getByRole("combobox", { name: "Operation", exact: true })
      .selectOption("image");
    await page
      .getByRole("textbox", {
        name: "Prompt Enter the prompt for the image operation.",
        exact: true,
      })
      .fill("Synthetic image");
    await button(page, "Run operation").click();
  },
});
scene("configuration-media-recovery", "/configuration", {
  action: async (page) => {
    await mediaSetup(page);
    await patch(page, {
      playgroundCreateMedia: {
        id: "content-job",
        logical_call_id: "content-call",
        selector: { provider_model_api_name: "route" },
        provider_model_api_name: "route",
        kind: "image",
        state: "running",
        attempts: [],
        created_at: date,
      },
    });
    await mode(page, { hold: ["playgroundMediaJob"] });
    await page
      .getByRole("combobox", { name: "Operation", exact: true })
      .selectOption("image");
    await page
      .getByRole("textbox", {
        name: "Prompt Enter the prompt for the image operation.",
        exact: true,
      })
      .fill("Synthetic image");
    await button(page, "Run operation").click();
  },
});
scene("overview-partial", "/overview", {
  action: async (page) => {
    await page.evaluate(() => {
      window.shellFixture.values.services.page = {
        has_more: true,
        next_cursor: "more",
      };
    });
    await button(page, "Refresh overview").click();
    await settle(page);
  },
});
scene("configuration-partial", "/configuration", {
  action: async (page) => {
    await page.evaluate(() => {
      window.shellFixture.values.providers.page = {
        has_more: true,
        next_cursor: "more",
      };
    });
    await button(page, "Refresh configuration").click();
    await settle(page);
  },
});
scene("details-has-children", "/services/{serviceApiName}", {
  path: "/services/child",
  values: {
    services: pageOf([
      {
        api_name: "root",
        is_root: true,
        display_name: "Root service",
        parent_service_api_name: null,
        created_at: date,
      },
      {
        api_name: "child",
        is_root: false,
        display_name: "Child service",
        parent_service_api_name: "root",
        created_at: date,
      },
      {
        api_name: "grandchild",
        display_name: "Grandchild",
        parent_service_api_name: "child",
        created_at: date,
      },
    ]),
  },
});
for (const [id, target, action] of [
  ["provider", "provider:provider", "Delete provider"],
  ["model", "model:model", "Delete model"],
  ["mapping", "mapping:route", "Delete provider route"],
  ["assignment", "assignment:assignment", "Delete local definition"],
])
  scene("configuration-delete-" + id, "/configuration", {
    path: "/configuration?service=root",
    action: async (page) => {
      await node(page, target);
      await button(page, action).click();
    },
  });
scene("configuration-delete-credential", "/configuration", {
  action: async (page) => {
    await button(page, "Add provider").click();
    await button(page, "Delete").click();
  },
});
scene("configuration-delete-requirement", "/configuration", {
  path: "/configuration?service=root",
  action: async (page) => {
    await node(page, "assignment:assignment");
    await button(page, "Remove").click();
  },
});
scene("operations-retention-reduce", "/operations", {
  action: async (page) => {
    let message;
    page.once("dialog", async (dialog) => {
      message = dialog.message();
      await dialog.dismiss();
    });
    await page
      .getByRole("spinbutton", { name: "Duration in whole days", exact: true })
      .fill("3");
    await button(page, "Save retention").click();
    await expect.poll(() => message).toBe(inventory.nativeDialogs[0].text);
    await writeFile(
      resolve(evidence, page.viewportSize().width + "-retention-dialog.json"),
      JSON.stringify(
        {
          type: "confirm",
          message,
          response: "dismiss",
          nativeDialogNotInDomScreenshot: true,
        },
        null,
        2,
      ) + "\n",
    );
  },
});

scene("statistics-results", "/statistics", {
  values: {
    statistics: {
      from: date,
      to: date,
      group_by: [],
      buckets: [
        {
          dimensions: [],
          calls: 2,
          attempts: 3,
          units: [{ unit: "input_token", quantity: "42" }],
          cost: "0.0042",
          currency: "USD",
        },
      ],
    },
  },
  action: async (page) => {
    await button(page, "Run statistics").click();
    await expect(
      page
        .getByText("USD 0.0042", { exact: true })
        .filter({ visible: true })
        .first(),
    ).toBeVisible();
  },
});
scene("operations-activity", "/operations", {
  values: {
    activityPage: pageOf([
      {
        id: "activity",
        actor_subject: "fixture-administrator",
        action: "service.created",
        resource_type: "service",
        resource_api_name: "child",
        result: "succeeded",
        occurred_at: date,
      },
    ]),
  },
  live: ["1 activity records loaded."],
});

for (const operation of ["embedding", "audio", "video"])
  scene("configuration-playground-" + operation, "/configuration", {
    action: async (page) => {
      await mediaSetup(page);
      await page
        .getByRole("combobox", { name: "Operation", exact: true })
        .selectOption(operation);
    },
  });
scene("configuration-credential-replace", "/configuration", {
  action: async (page) => {
    await button(page, "Add provider").click();
    await page
      .getByRole("textbox", { name: "Credential API name", exact: true })
      .fill("credential");
    await page
      .getByRole("textbox", { name: "New secret", exact: true })
      .fill("synthetic-secret");
    await button(page, "Save credential").click();
  },
});
scene("configuration-draft-discard", "/configuration", {
  path: "/configuration?service=root",
  action: async (page) => {
    await button(page, "Add assignment").click();
    await page
      .getByRole("textbox", { name: "Assignment API name", exact: true })
      .fill("draft");
    await button(page, "Discard changes").click();
  },
});
scene("details-access-loading", "/services/{serviceApiName}", {
  hold: ["workspaces", "keys"],
});
scene("details-write-error", "/services/{serviceApiName}", {
  fail: ["updateService"],
  action: async (page) => {
    await page
      .getByRole("textbox", { name: "Display name", exact: true })
      .fill("Changed root");
    await button(page, "Save changes").click();
  },
});
scene("logs-invalid-filter", "/logs", {
  action: async (page) => {
    await page.locator("#logs-filter-from").fill("2000-01-01T00:00:00");
    await button(page, "Apply Logs filters").click();
  },
});
for (const width of [1440, 390])
  for (const item of scenes)
    it(`${width} ${item.id}`, { timeout: 30000 }, async () => {
      const { context, page, errors } = await open(width, item);
      try {
        await item.action?.(page);
        await proof(page, item, width);
      } finally {
        await context.close();
        assert.deepEqual(errors, []);
      }
    });
it("inventory has complete route and keep-reason coverage", () => {
  assert.equal(inventory.routes.length, 7);
  for (const width of [1440, 390])
    assert.deepEqual(
      results
        .filter((result) => result.width === width)
        .map((result) => result.scene)
        .sort(),
      scenes.map((scene) => scene.id).sort(),
    );
  for (const route of inventory.routes)
    for (const item of route.items) {
      if (item.expected === "present") {
        assert.ok(inventory.keepReasons.includes(item.reason), item.id);
        assert.ok(item.scenes.length, item.id);
        for (const id of item.scenes)
          assert.ok(
            scenes.some((scene) => scene.id === id),
            id,
          );
      }
    }
  for (const reason of inventory.keepReasons)
    assert.ok(
      inventory.routes.some((route) =>
        route.items.some((item) => item.reason === reason),
      ),
      reason,
    );
});

it(
  "content comparator rejects a prohibited helper and a missing retained fact",
  { timeout: 20000 },
  async () => {
    const item = scenes.find((item) => item.id === "operations-normal");
    const { context, page, errors } = await open(1440, item);
    try {
      const forbidden = inventory.routes
        .flatMap((route) => route.items)
        .find((item) => item.id === "activity-disclaimer").text;
      await page.evaluate((text) => {
        const p = document.createElement("p");
        p.id = "injected-helper";
        p.textContent = text;
        document.querySelector("main").append(p);
      }, forbidden);
      await assert.rejects(() => proof(page, item, 1440), /toHaveCount/);
      await page.locator("#injected-helper").evaluate((node) => node.remove());
      await page
        .getByText(
          "The duration applies to detailed logs, activity, uploaded images, and retained generated media.",
          { exact: true },
        )
        .evaluate((node) => (node.textContent = "Changed retention fact"));
      await assert.rejects(() => proof(page, item, 1440), /toBeVisible/);
    } finally {
      await context.close();
      assert.deepEqual(errors, []);
    }
  },
);
