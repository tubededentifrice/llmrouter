// Run: node apps/admin/test/configuration-forms.browser.mjs
/* global window, innerWidth, innerHeight */
// The real App uses a controlled localhost client. No session, server write, or provider call.
import { existsSync } from "node:fs";
import { reloadFixture } from "./browserControls.mjs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { after, before, describe, it } from "node:test";
import { URL } from "node:url";

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
  process.env.LLMROUTER_FORMS_EVIDENCE ??
    "/tmp/llmrouter-configuration-forms-browser",
);
const date = "2026-08-25T00:00:00Z";
const direct = {
  api_name: "workflow",
  display_name: "Workflow",
  definition_kind: "direct_chain",
  defined_by_service_api_name: "child",
  direct_chain: [{ provider_model_api_name: "route" }],
  effective_chain: [{ provider_model_api_name: "route" }],
  observed_requirements: [],
  created_at: date,
};
const target = {
  ...direct,
  api_name: "default",
  display_name: "Default assignment",
  defined_by_service_api_name: "root",
};
const pageValue = (items) => ({ items, page: { has_more: false } });
let browser;
let script;
let css;
const measurements = [];

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
    resolve(evidence, "measurements.json"),
    JSON.stringify(measurements, null, 2),
  );
});

async function open(width, height) {
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    expect(url.origin).toBe("http://127.0.0.1:5174");
    if (url.pathname === "/forms-fixture.js")
      return route.fulfill({ contentType: "text/javascript", body: script });
    if (url.pathname === "/forms-fixture.css")
      return route.fulfill({ contentType: "text/css", body: css });
    return route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Configuration form fixture</title><link rel="stylesheet" href="/forms-fixture.css"></head><body><div id="root"></div><script>window.shellBoot=${JSON.stringify({ hold: [], fail: [], signedOut: false, values: { assignments: pageValue([direct, target]) } })}</script><script src="/forms-fixture.js"></script></body></html>`,
    });
  });
  await page.goto("http://127.0.0.1:5174/configuration?service=child");
  await expect(
    page.locator('[data-node-id="provider:provider"]'),
  ).toBeVisible();
  await expect(
    page.locator('[data-node-id="assignment:workflow"]'),
  ).toBeVisible();
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  return { context, page, errors };
}
const cases = [
  {
    kind: "provider",
    add: "Add provider",
    id: "provider:provider",
    title: "Fixture provider",
    identity: "API name",
    focus: "Display name",
    save: "Save provider",
  },
  {
    kind: "model",
    add: "Add canonical model",
    id: "model:model",
    title: "Fixture model",
    identity: "API name",
    focus: "Display name",
    save: "Save model",
  },
  {
    kind: "mapping",
    add: "Add provider route",
    id: "mapping:route",
    title: "Fixture provider",
    identity: "Route API name",
    focus: "Provider wire model",
    save: "Save route",
  },
  {
    kind: "assignment",
    add: "Add assignment",
    id: "assignment:workflow",
    title: "Workflow",
    identity: "Assignment API name",
    focus: "Display name",
    save: "Save assignment",
  },
];
async function edit(page, item, create) {
  if (create)
    await page.getByRole("button", { name: item.add, exact: true }).click();
  else {
    const node = page.locator(`[data-node-id="${item.id}"]`);
    await node.click();
    await node
      .locator("..")
      .getByRole("button", { name: /^Edit / })
      .click();
  }
  const dialog = page.locator(".configuration-edit-dialog[open]");
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByRole("textbox", {
      name: create ? item.identity : item.focus,
      exact: true,
    }),
  ).toBeFocused();
  return dialog;
}
async function finish(context, errors) {
  await context.close();
  expect(errors).toEqual([]);
}
async function assertLayout(page, dialog, item, label) {
  const save = dialog.getByRole("button", { name: item.save, exact: true });
  const cancel = dialog.getByRole("button", { name: "Cancel", exact: true });
  await expect(save).toBeVisible();
  await expect(cancel).toBeVisible();
  const initial = await dialog.evaluate((element) => {
    const body = element.querySelector(".od-dialog-body");
    const first = body.querySelector(
      "input:not([type=hidden]), select, textarea",
    );
    const facts = body.querySelector(".od-graph-inspector-facts");
    const bounds = element.getBoundingClientRect();
    const footer = element
      .querySelector(".od-dialog-actions")
      .getBoundingClientRect();
    return {
      dialogWidth: bounds.width,
      dialogRight: bounds.right,
      dialogBottom: bounds.bottom,
      bodyWidth: body.clientWidth,
      bodyScrollWidth: body.scrollWidth,
      footerTop: footer.top,
      footerBottom: footer.bottom,
      firstBeforeFacts:
        !facts || Boolean(first.compareDocumentPosition(facts) & 4),
      viewportWidth: innerWidth,
      viewportHeight: innerHeight,
    };
  });
  expect(initial.dialogRight).toBeLessThanOrEqual(initial.viewportWidth);
  expect(initial.dialogBottom).toBeLessThanOrEqual(initial.viewportHeight);
  expect(initial.bodyScrollWidth).toBeLessThanOrEqual(initial.bodyWidth);
  expect(initial.firstBeforeFacts).toBe(true);
  expect(initial.footerBottom).toBeLessThanOrEqual(initial.viewportHeight);
  expect(await save.evaluate((element) => element.form?.id)).toBe(
    `configuration-${item.kind}-form`,
  );
  // Expand optional fields and scroll the body. Main actions must keep their position.
  await dialog.locator("details").evaluateAll((items) => {
    for (const item of items) item.open = true;
  });
  const expandedFooterTop = await dialog
    .locator(".od-dialog-actions")
    .evaluate((element) => element.getBoundingClientRect().top);
  await dialog.locator(".od-dialog-body").evaluate((body) => {
    body.scrollTop = body.scrollHeight;
  });
  expect(
    await dialog
      .locator(".od-dialog-actions")
      .evaluate((element) => element.getBoundingClientRect().top),
  ).toBe(expandedFooterTop);
  await save.click({ trial: true });
  await cancel.click({ trial: true });
  await dialog.locator(".od-dialog-body").evaluate((body) => {
    body.scrollTop = 0;
  });
  await dialog.locator("details").evaluateAll((items) => {
    for (const item of items) item.open = false;
  });
  const accessibility = await new AxeBuilder({ page })
    .include(".configuration-edit-dialog")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
  measurements.push({
    label,
    ...initial,
    axeViolations: accessibility.violations.length,
  });
  await page.screenshot({ path: resolve(evidence, `${label}.png`) });
}

for (const [width, height] of [
  [1440, 900],
  [390, 844],
]) {
  describe(`Configuration forms at ${width}px`, () => {
    for (const auxiliary of [
      {
        kind: "credential",
        item: cases[0],
        create: false,
        summary: "Manage credentials",
        method: "createCredential",
        submit: "Save credential",
      },
      {
        kind: "import",
        item: cases[1],
        create: true,
        summary: "Import from OpenRouter",
        method: "previewOpenRouterModel",
        submit: "Preview OpenRouter model",
      },
    ]) {
      it(`${auxiliary.kind} pending request locks all modal forms`, async () => {
        const { context, page, errors } = await open(width, height);
        try {
          const dialog = await edit(page, auxiliary.item, auxiliary.create);
          await dialog
            .locator("summary", { hasText: auxiliary.summary })
            .click();
          if (auxiliary.kind === "credential") {
            await dialog
              .getByLabel("Credential API name", { exact: true })
              .fill("new-credential");
            await dialog
              .getByLabel("New secret", { exact: true })
              .fill("synthetic-test-secret");
          } else {
            await dialog
              .getByLabel("Exact model ID or supported OpenRouter URL", {
                exact: true,
              })
              .fill("fixture/model");
          }
          await page.evaluate((name) => {
            window.shellFixture.values[name] = {};
            window.shellFixture.hold.push(name);
          }, auxiliary.method);
          await dialog
            .getByRole("button", { name: auxiliary.submit, exact: true })
            .click();
          await expect
            .poll(() =>
              page.evaluate(
                (name) =>
                  window.shellFixture.pending.some(
                    (operation) => operation.name === name,
                  ),
                auxiliary.method,
              ),
            )
            .toBe(true);
          for (const control of await dialog
            .locator(
              'form input:not([type="hidden"]), form select, form textarea, form button',
            )
            .all())
            await expect(control).toBeDisabled();
          await page.evaluate((name) => {
            window.shellFixture.hold = window.shellFixture.hold.filter(
              (held) => held !== name,
            );
            window.shellFixture.finish(name, true);
          }, auxiliary.method);
          await expect(
            dialog.getByRole("button", { name: auxiliary.submit, exact: true }),
          ).toBeEnabled();
          if (auxiliary.kind === "credential")
            await expect(
              dialog.getByLabel("New secret", { exact: true }),
            ).toHaveValue("");
          else
            await expect(
              dialog.getByLabel("Exact model ID or supported OpenRouter URL", {
                exact: true,
              }),
            ).toHaveValue("fixture/model");
        } finally {
          await finish(context, errors);
        }
      });
    }
    for (const item of cases) {
      it(`${item.kind} held save locks controls, close actions, and submitted values`, async () => {
        const { context, page, errors } = await open(width, height);
        try {
          if (item.kind === "assignment") {
            await page.evaluate(() => {
              const fixture = window.shellFixture;
              fixture.values.providerModels.items.push({
                ...fixture.values.providerModels.items[0],
                api_name: "available-route",
                provider_model_name: "wire/available",
              });
            });
            await reloadFixture(page);
            await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
          }
          const dialog = await edit(page, item, false);
          const changed = dialog.getByRole("textbox", {
            name: item.focus,
            exact: true,
          });
          await changed.fill(`Pending ${item.kind}`);
          const method = {
            provider: "putProvider",
            model: "putModel",
            mapping: "putProviderModel",
            assignment: "putAssignment",
          }[item.kind];
          await page.evaluate((name) => {
            window.shellFixture.values[name] = {};
            window.shellFixture.hold.push(name);
          }, method);
          const form = dialog.locator(`#configuration-${item.kind}-form`);
          if (item.kind === "assignment")
            await expect(
              dialog.getByRole("combobox", {
                name: "Add provider route",
                exact: true,
              }),
            ).toBeEnabled();
          await dialog
            .getByRole("button", { name: item.save, exact: true })
            .click();
          await expect
            .poll(() =>
              page.evaluate(
                (name) =>
                  window.shellFixture.pending.some(
                    (operation) => operation.name === name,
                  ),
                method,
              ),
            )
            .toBe(true);
          const submitted = await page.evaluate(
            (name) =>
              window.shellFixture.calls.find((call) => call.name === name).args,
            method,
          );
          const controls = form.locator(
            'input:not([type="hidden"]), select, textarea, button',
          );
          expect(await controls.count()).toBeGreaterThan(2);
          for (const control of await controls.all())
            await expect(control).toBeDisabled();
          await expect(
            dialog.getByRole("button", { name: "Cancel", exact: true }),
          ).toBeDisabled();
          await expect(
            dialog.getByRole("button", { name: "Close dialog", exact: true }),
          ).toBeDisabled();
          await expect(
            dialog.getByRole("button", { name: "Saving…", exact: true }),
          ).toBeDisabled();
          await expect(
            changed.fill("Changed during save", { timeout: 150 }),
          ).rejects.toThrow();
          await expect(changed).toHaveValue(`Pending ${item.kind}`);
          if (item.kind === "assignment") {
            await expect(
              dialog.getByRole("combobox", {
                name: "Add provider route",
                exact: true,
              }),
            ).toBeDisabled();
            await expect(
              dialog.getByRole("button", {
                name: "Remove fixture/model",
                exact: true,
              }),
            ).toBeDisabled();
          }
          await page.keyboard.press("Escape");
          await expect(dialog).toBeVisible();
          await expect(
            page.getByRole("dialog", {
              name: "Discard assignment changes?",
              exact: true,
            }),
          ).toHaveCount(0);
          const current = await page.evaluate(
            (name) =>
              window.shellFixture.calls
                .filter((call) => call.name === name)
                .map((call) => call.args),
            method,
          );
          expect(current).toEqual([submitted]);
          expect(submitted[item.kind === "assignment" ? 2 : 1]).toMatchObject(
            item.kind === "mapping"
              ? { provider_model_name: "Pending mapping" }
              : { display_name: `Pending ${item.kind}` },
          );
          // Release as a controlled failure. The same draft must become editable again.
          await page.evaluate((name) => {
            window.shellFixture.hold = window.shellFixture.hold.filter(
              (held) => held !== name,
            );
            window.shellFixture.finish(name, true);
          }, method);
          await expect(changed).toBeEnabled();
          await expect(changed).toHaveValue(`Pending ${item.kind}`);
          await expect(
            dialog.getByRole("button", { name: item.save, exact: true }),
          ).toBeEnabled();
          await expect(
            dialog.getByRole("button", { name: "Cancel", exact: true }),
          ).toBeEnabled();
          await expect(
            dialog.getByRole("button", { name: "Close dialog", exact: true }),
          ).toBeEnabled();
        } finally {
          await finish(context, errors);
        }
      });
      for (const create of [false, true]) {
        it(`${create ? "add" : "edit"} ${item.kind}: focus, controls, footer, and accessibility`, async () => {
          const { context, page, errors } = await open(width, height);
          try {
            const dialog = await edit(page, item, create);
            await assertLayout(
              page,
              dialog,
              item,
              `${width}-${create ? "add" : "edit"}-${item.kind}`,
            );
            await dialog
              .getByRole("button", { name: "Cancel", exact: true })
              .click();
            await expect(dialog).toHaveCount(0);
          } finally {
            await finish(context, errors);
          }
        });
      }
    }
    it("required custom endpoint is visible and browser-validated", async () => {
      const { context, page, errors } = await open(width, height);
      try {
        const dialog = await edit(page, cases[0], true);
        await dialog
          .getByLabel("Adapter", { exact: true })
          .selectOption("custom");
        const endpoint = dialog.getByLabel("Custom endpoint", { exact: true });
        await expect(endpoint).toBeVisible();
        expect(await endpoint.evaluate((element) => element.type)).toBe("url");
        expect(await endpoint.evaluate((element) => element.required)).toBe(
          true,
        );
        await dialog
          .getByLabel("API name", { exact: true })
          .fill("new-provider");
        await dialog
          .getByLabel("Display name", { exact: true })
          .fill("New provider");
        await dialog
          .getByRole("button", { name: "Save provider", exact: true })
          .click();
        expect(
          await page.evaluate(() =>
            window.shellFixture.calls.some(
              (call) => call.name === "createProvider",
            ),
          ),
        ).toBe(false);
        expect(
          await endpoint.evaluate((element) => element.checkValidity()),
        ).toBe(false);
      } finally {
        await finish(context, errors);
      }
    });
    it("model checkbox choices submit exact input, output, and capability values", async () => {
      const { context, page, errors } = await open(width, height);
      try {
        const dialog = await edit(page, cases[1], false);
        const input = dialog.getByRole("group", { name: "Input", exact: true });
        const output = dialog.getByRole("group", {
          name: "Output",
          exact: true,
        });
        const capabilities = dialog.getByRole("group", {
          name: "Capabilities",
          exact: true,
        });
        await input
          .getByRole("checkbox", { name: "Text", exact: true })
          .uncheck();
        await input
          .getByRole("checkbox", { name: "Image", exact: true })
          .check();
        await output
          .getByRole("checkbox", { name: "Text", exact: true })
          .uncheck();
        await output
          .getByRole("checkbox", { name: "Audio", exact: true })
          .check();
        await capabilities
          .getByRole("checkbox", { name: "Streaming", exact: true })
          .uncheck();
        await capabilities
          .getByRole("checkbox", { name: "Reasoning", exact: true })
          .check();
        await page.evaluate(() => {
          window.shellFixture.values.putModel = {};
          window.shellFixture.hold.push("putModel");
        });
        await dialog
          .getByRole("button", { name: "Save model", exact: true })
          .click();
        await expect
          .poll(() =>
            page.evaluate(
              () =>
                window.shellFixture.calls.find(
                  (call) => call.name === "putModel",
                )?.args[1],
            ),
          )
          .toMatchObject({
            input_modalities: ["image"],
            output_modalities: ["audio"],
            capabilities: ["reasoning"],
          });
      } finally {
        await finish(context, errors);
      }
    });
    it("adds distinct routes directly, excludes duplicates, and saves remove/reorder changes", async () => {
      const { context, page, errors } = await open(width, height);
      try {
        await page.evaluate(() => {
          const fixture = window.shellFixture;
          const route = fixture.values.providerModels.items[0];
          fixture.values.providerModels.items.push(
            {
              ...route,
              api_name: "second-route",
              provider_model_name: "wire/alternate",
            },
            {
              ...route,
              api_name: "third-route",
              provider_model_name: "wire/third",
            },
          );
          fixture.hold.push("putAssignment");
        });
        await reloadFixture(page);
        await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
        const dialog = await edit(page, cases[3], false);
        const list = dialog.getByRole("list", {
          name: "Ordered assignment provider-route chain",
          exact: true,
        });
        const rows = list.locator(".od-ordered-choice-item");
        const add = dialog.getByRole("combobox", {
          name: "Add provider route",
          exact: true,
        });
        await expect(rows).toHaveCount(1);
        await expect(dialog.locator(".od-editable-table")).toHaveCount(0);
        await add.click();
        const choices = dialog.locator(".od-searchable-select-listbox");
        await expect(
          choices.getByRole("option", {
            name: "fixture/model · Fixture provider",
            exact: true,
          }),
        ).toHaveCount(0);
        await expect(
          choices.getByRole("option", {
            name: "wire/alternate · Fixture provider",
            exact: true,
          }),
        ).toHaveCount(1);
        await expect(
          choices.getByRole("option", {
            name: "wire/third · Fixture provider",
            exact: true,
          }),
        ).toHaveCount(1);
        await add.fill("alternate");
        await expect(choices.getByRole("option")).toHaveCount(1);
        await choices.selectOption("second-route");
        await expect(rows).toHaveCount(2);
        await expect(rows.nth(1).locator("strong")).toHaveText(
          "wire/alternate",
        );
        await expect(add).toHaveValue("");
        await add.click();
        await expect(
          choices.getByRole("option", {
            name: "wire/alternate · Fixture provider",
            exact: true,
          }),
        ).toHaveCount(0);
        await choices.selectOption("third-route");
        await expect(rows).toHaveCount(3);
        await expect(add).toBeDisabled();
        const dimensions = await rows.evaluateAll((items) =>
          items.map((item) => {
            const box = item.getBoundingClientRect();
            const name = item.querySelector(".od-ordered-choice-name");
            const model = name.querySelector("strong").getBoundingClientRect();
            const provider = name.querySelector("span").getBoundingClientRect();
            return {
              height: box.height,
              width: box.width,
              clientWidth: item.clientWidth,
              scrollWidth: item.scrollWidth,
              singleLine: Math.abs(model.top - provider.top) < 2,
              actionsSingleLine: [
                ...item.querySelectorAll(".od-ordered-choice-actions button"),
              ].every(
                (button, index, buttons) =>
                  Math.abs(
                    button.getBoundingClientRect().top -
                      buttons[0].getBoundingClientRect().top,
                  ) < 2,
              ),
            };
          }),
        );
        await page.screenshot({
          path: resolve(
            evidence,
            `${width}-ordered-route-chain-before-reorder.png`,
          ),
        });
        for (const dimensionsOfRow of dimensions) {
          expect(dimensionsOfRow.height).toBeLessThanOrEqual(58);
          expect(dimensionsOfRow.scrollWidth).toBeLessThanOrEqual(
            dimensionsOfRow.clientWidth,
          );
          if (width > 390) expect(dimensionsOfRow.singleLine).toBe(true);
          expect(dimensionsOfRow.actionsSingleLine).toBe(true);
        }
        for (const action of await list.getByRole("button").all()) {
          await expect(action).toHaveClass(/od-icon-button/);
          await expect(action.locator("svg")).toHaveCount(1);
          expect(await action.textContent()).toBe("");
        }
        await list
          .getByRole("button", { name: "Move wire/third up", exact: true })
          .click();
        await expect(rows.locator("strong")).toHaveText([
          "fixture/model",
          "wire/third",
          "wire/alternate",
        ]);
        const handle = list.getByRole("button", {
          name: "Reorder wire/third",
          exact: true,
        });
        await handle.focus();
        await page.keyboard.press("ArrowUp");
        await expect(rows.locator("strong")).toHaveText([
          "wire/third",
          "fixture/model",
          "wire/alternate",
        ]);
        await expect(handle).toBeFocused();
        await list
          .getByRole("button", { name: "Remove fixture/model", exact: true })
          .click();
        await expect(rows.locator("strong")).toHaveText([
          "wire/third",
          "wire/alternate",
        ]);
        // Keep remaining draft rows when the chain editor is mounted again.
        const definition = dialog.getByLabel("Definition", { exact: true });
        await definition.selectOption("inherit");
        await expect(list).toHaveCount(0);
        await definition.selectOption("direct");
        await expect(rows.locator("strong")).toHaveText([
          "wire/third",
          "wire/alternate",
        ]);
        await add.click();
        await choices.selectOption("route");
        await expect(rows).toHaveCount(3);
        await list
          .getByRole("button", { name: "Remove fixture/model", exact: true })
          .click();
        await expect(rows).toHaveCount(2);
        await add.click();
        await expect(
          choices.getByRole("option", {
            name: "fixture/model · Fixture provider",
            exact: true,
          }),
        ).toHaveCount(1);
        await expect(
          choices.getByRole("option", {
            name: "wire/alternate · Fixture provider",
            exact: true,
          }),
        ).toHaveCount(0);
        await page.keyboard.press("Escape");
        const accessibility = await new AxeBuilder({ page })
          .include(".configuration-edit-dialog")
          .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
          .analyze();
        expect(accessibility.violations).toEqual([]);
        measurements.push({
          label: `${width}-ordered-route-chain`,
          rows: dimensions,
          axeViolations: accessibility.violations.length,
        });
        await page.screenshot({
          path: resolve(evidence, `${width}-ordered-route-chain.png`),
        });
        await dialog
          .getByRole("button", { name: "Save assignment", exact: true })
          .click();
        await expect
          .poll(() =>
            page.evaluate(
              () =>
                window.shellFixture.calls.find(
                  (call) => call.name === "putAssignment",
                )?.args[2],
            ),
          )
          .toMatchObject({
            direct_chain: [
              { provider_model_api_name: "third-route" },
              { provider_model_api_name: "second-route" },
            ],
          });
      } finally {
        await finish(context, errors);
      }
    });
    it("searchable inheritance submits the selected assignment API name", async () => {
      const { context, page, errors } = await open(width, height);
      try {
        const dialog = await edit(page, cases[3], false);
        await dialog
          .getByLabel("Definition", { exact: true })
          .selectOption("inherit");
        const inherited = dialog.getByRole("combobox", {
          name: "Inherited assignment",
          exact: true,
        });
        await inherited.fill("Default");
        const options = dialog.locator(".od-searchable-select-listbox");
        await expect(
          options.getByRole("option", {
            name: "Default assignment",
            exact: true,
          }),
        ).toHaveCount(1);
        await expect(
          options.getByRole("option", { name: "Workflow", exact: true }),
        ).toHaveCount(0);
        await options.selectOption("default");
        await expect(inherited).toHaveValue("Default assignment");
        await page.evaluate(() => {
          window.shellFixture.hold.push("putAssignment");
        });
        await dialog
          .getByRole("button", { name: "Save assignment", exact: true })
          .click();
        await expect
          .poll(() =>
            page.evaluate(
              () =>
                window.shellFixture.calls.find(
                  (call) => call.name === "putAssignment",
                )?.args,
            ),
          )
          .toEqual([
            "child",
            "workflow",
            {
              display_name: "Workflow",
              inherits_assignment_api_name: "default",
            },
            "synthetic-csrf",
          ]);
      } finally {
        await finish(context, errors);
      }
    });
  });
}
