// Run: node apps/admin/test/service-creation.browser.mjs
/* global window, document, innerHeight, getComputedStyle, requestAnimationFrame, Event */
// Real App with controlled client responses. Every browser request is intercepted.
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { URL } from "node:url";
import { after, before, describe, it } from "node:test";
const root = resolve(import.meta.dirname, "../../..");
const toolRoot =
  process.env.OPENDLE_BROWSER_TOOLS ??
  (existsSync(resolve(root, "../opendle-ui/node_modules/@playwright/test"))
    ? resolve(root, "../opendle-ui")
    : "/home/ubuntu/git/opendle-ui");
const requireTools = createRequire(resolve(toolRoot, "package.json"));
const { chromium, expect } = requireTools("@playwright/test");
const { build } = requireTools("esbuild");
const { default: AxeBuilder } = requireTools("@axe-core/playwright");
const shared = dirname(
  createRequire(import.meta.url).resolve("@opendle/ui/package.json"),
);
const evidence = resolve(
  process.env.LLMROUTER_CREATION_EVIDENCE ?? "/tmp/llmr-h77t-browser",
);
const measured = [];
const sizes = [
  [1440, 1000],
  [1100, 800],
  [390, 844],
];
let browser, script, css;
before(async () => {
  const result = await build({
    entryPoints: [
      resolve(root, "apps/admin/test/fixtures/service-creation-browser.tsx"),
    ],
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
  script = result.outputFiles[0].text;
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
    JSON.stringify(measured, null, 2),
  );
});
const node = (page, name) => page.locator(`[data-service-api-name="${name}"]`);
const action = (page, name = "Root service", api = "root") =>
  page.getByRole("button", {
    name: `New service under ${name}, API name ${api}`,
    exact: true,
  });
const heading = (page) =>
  page.getByRole("heading", { name: "New service", exact: true });
const inspector = (page) => page.locator(".od-graph-inspector");
const submit = (page) =>
  page.getByRole("button", { name: "Create service", exact: true });
const refresh = (page) =>
  page.getByRole("button", { name: "Refresh services", exact: true });
async function open(width, height, boot = {}) {
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 1,
    hasTouch: width === 390,
    serviceWorkers: "block",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://127.0.0.1:5174") {
      errors.push(`Unexpected request origin: ${url.origin}`);
      return route.abort();
    }
    if (url.pathname === "/creation-fixture.js")
      return route.fulfill({ contentType: "text/javascript", body: script });
    if (url.pathname === "/creation-fixture.css")
      return route.fulfill({ contentType: "text/css", body: css });
    if (!/^\/(services|overview)(\/[^/]*)?$/.test(url.pathname)) {
      errors.push(`Unexpected request path: ${url.pathname}`);
      return route.abort();
    }
    return route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Service creation fixture</title><link rel="stylesheet" href="/creation-fixture.css"></head><body><div id="root"></div><script>window.creationBoot=${JSON.stringify(boot).replaceAll("<", "\\u003c")}</script><script src="/creation-fixture.js"></script></body></html>`,
    });
  });
  await page.goto(`http://127.0.0.1:5174${boot.path ?? "/services"}`);
  await expect(page.locator("main h1")).toHaveText(
    boot.path === "/overview" ? "Overview" : "Services",
  );
  return { page, context, errors };
}
async function close(context, errors) {
  await context.close();
  expect(errors).toEqual([]);
}
async function ready(page) {
  await expect(action(page)).toBeVisible();
}
async function finish(page, name, result, error) {
  await page.evaluate(
    ({ name, result, error }) =>
      window.creationFixture.finish(name, result, error),
    { name, result, error },
  );
}
async function hold(page, names) {
  await page.evaluate((names) => {
    window.creationFixture.hold = names;
  }, names);
}
async function count(page, name) {
  return page.evaluate(
    (name) =>
      window.creationFixture.calls.filter((call) => call.name === name).length,
    name,
  );
}
async function pending(page, name) {
  await expect
    .poll(() =>
      page.evaluate(
        (name) =>
          window.creationFixture.pending.filter((call) => call.name === name)
            .length,
        name,
      ),
    )
    .toBeGreaterThan(0);
}
async function list(page, removeParent = false, partial = false) {
  return page.evaluate(
    ({ removeParent, partial }) => ({
      items: window.creationFixture.services.filter(
        (service) => !removeParent || service.api_name !== "parent",
      ),
      page: { has_more: partial },
    }),
    { removeParent, partial },
  );
}
async function draft(page, parent = "root") {
  if (parent !== "root") {
    await node(page, parent).click();
    await expect(page.locator(".od-graph-inspector h2")).toHaveText(
      "Parent service",
    );
    await page.keyboard.press("Escape");
  }
  await action(
    page,
    parent === "root" ? "Root service" : "Parent service",
    parent,
  ).click();
  await expect(heading(page)).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("textbox", { name: "Display name", exact: true }),
  ).toBeFocused();
  await expect(inspector(page).getByRole("textbox")).toHaveCount(2);
  await expect(inspector(page).getByRole("combobox")).toHaveCount(0);
  await expect(inspector(page).locator("dd")).toContainText(
    parent === "root" ? "Root service root" : "Parent service parent",
  );
}
async function fill(page) {
  await page
    .getByRole("textbox", { name: "Display name", exact: true })
    .fill("New child");
  await page
    .getByRole("textbox", { name: "API name", exact: true })
    .fill("new-child");
}
async function start(page) {
  await fill(page);
  await submit(page).click(); // Keep the browser's actual submit-to-disabled focus transition.
  await pending(page, "createService");
  await expect(inspector(page)).toHaveAttribute("aria-busy", "true");
  await expect(
    inspector(page).getByText("Creating service", { exact: true }),
  ).toBeVisible();
  for (const control of await inspector(page).locator("input, button").all())
    await expect(control).toBeDisabled();
}
function near(actual, expected, label) {
  expect(
    Math.abs(actual - expected),
    `${label}: ${actual} / ${expected}`,
  ).toBeLessThanOrEqual(1);
}
async function evidenceFor(page, state) {
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  const data = await page.evaluate(() => {
    const box = (element) => {
      const b = element.getBoundingClientRect();
      return {
        x: b.x,
        y: b.y,
        width: b.width,
        height: b.height,
        right: b.right,
        bottom: b.bottom,
      };
    };
    const host = document.querySelector(".od-graph-workspace"),
      panel = document.querySelector(".od-graph-inspector");
    const nav = document.querySelector(".od-application-mobile-navigation"),
      view = document.querySelector(".od-graph-viewport");
    const doc = document.scrollingElement;
    return {
      host: box(host),
      view: box(view),
      nav: box(nav),
      toolbar: box(document.querySelector(".od-graph-toolbar")),
      panel: panel ? box(panel) : null,
      mode: panel?.dataset.mode,
      modal: panel?.matches(":modal"),
      rem: parseFloat(getComputedStyle(document.documentElement).fontSize),
      viewportHeight: innerHeight,
      document: [
        doc.scrollWidth,
        doc.clientWidth,
        doc.scrollHeight,
        doc.clientHeight,
      ],
    };
  });
  near(data.host.y, 0, "graph top");
  near(
    data.host.bottom,
    data.nav.height ? data.nav.y : data.viewportHeight,
    "graph bottom",
  );
  expect(data.document[0]).toBeLessThanOrEqual(data.document[1]);
  expect(data.document[2]).toBeLessThanOrEqual(data.document[3]);
  if (data.panel) {
    const expectedMode =
      data.host.width >= 69 * data.rem
        ? "split"
        : data.host.width > 48 * data.rem
          ? "overlay"
          : "sheet";
    expect(data.mode).toBe(expectedMode);
    expect(data.modal).toBe(expectedMode === "sheet");
    if (expectedMode !== "sheet")
      near(data.panel.width, 21 * data.rem, "inspector width");
    if (expectedMode === "overlay") {
      near(
        data.host.right - data.panel.right,
        0.875 * data.rem,
        "overlay right",
      );
      near(
        data.panel.y,
        Math.max(
          data.host.y + 4.75 * data.rem,
          data.toolbar.bottom + 0.875 * data.rem,
        ),
        "overlay top",
      );
    }
    if (expectedMode === "sheet") {
      near(data.panel.x, 0.75 * data.rem, "sheet left");
      near(
        data.viewportHeight - data.panel.bottom,
        0.75 * data.rem,
        "sheet bottom",
      );
    }
  }
  const axe = await new AxeBuilder({ page }).analyze();
  await writeFile(
    resolve(evidence, `${state}.axe.json`),
    JSON.stringify(axe.violations, null, 2),
  );
  await page.screenshot({
    path: resolve(evidence, `${state}.png`),
    fullPage: true,
  });
  measured.push({ state, ...data, violations: axe.violations });
  expect(axe.violations, `Axe: ${state}`).toEqual([]);
}
async function placement(page, parent = "root") {
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  const opener = action(
    page,
    parent === "root" ? "Root service" : "Parent service",
    parent,
  );
  await expect(
    page
      .locator(".od-graph-toolbar")
      .getByRole("button", { name: /New service|Create service/ }),
  ).toHaveCount(0);
  await expect(
    page
      .locator(".od-graph-canvas")
      .getByRole("button", { name: /^New service under/ }),
  ).toHaveCount(1);
  await expect(opener).toHaveText("+ New service");
  const p = await node(page, parent).boundingBox(),
    a = await opener.boundingBox();
  expect(a.y).toBeGreaterThanOrEqual(p.y + p.height);
  near(a.x + a.width / 2, p.x + p.width / 2, "action centered under parent");
  const boxes = await page
    .locator("[data-service-api-name]")
    .evaluateAll((nodes) =>
      nodes.map((n) => {
        const b = n.getBoundingClientRect();
        return { x: b.x, y: b.y, right: b.right, bottom: b.bottom };
      }),
    );
  for (const b of boxes)
    expect(
      a.x >= b.right ||
        a.x + a.width <= b.x ||
        a.y >= b.bottom ||
        a.y + a.height <= b.y,
      "action must not overlap any node",
    ).toBe(true);
  for (const control of [node(page, parent), opener]) {
    await control.scrollIntoViewIfNeeded();
    await control.click({ trial: true });
    expect(
      await control.evaluate((e) => {
        const b = e.getBoundingClientRect();
        return [
          [b.left + b.width / 2, b.top + 2],
          [b.right - 2, b.top + b.height / 2],
          [b.left + 2, b.top + b.height / 2],
          [b.left + b.width / 2, b.bottom - 2],
          [b.left + b.width / 2, b.top + b.height / 2],
        ].every(([x, y]) => {
          const hit = document.elementFromPoint(x, y);
          return hit === e || e.contains(hit);
        });
      }),
    ).toBe(true);
  }
  await expect(page.locator('.od-graph-canvas [tabindex="0"]')).toHaveCount(1);
}
async function success(page) {
  await finish(page, "createService");
  await expect(page.locator(".od-graph-inspector h2")).toHaveText("New child");
  await expect(page.locator(".od-graph-inspector h2")).toBeFocused();
  await expect(node(page, "new-child")).toHaveAttribute("aria-pressed", "true");
  expect(new URL(page.url()).pathname).toBe("/services");
  if ((await inspector(page).getAttribute("data-mode")) === "overlay") {
    await writeFile(
      resolve(evidence, "success-overlay-probe.json"),
      JSON.stringify(
        await page.evaluate(() => {
          const box = (e) => {
            const b = e.getBoundingClientRect();
            return {
              left: b.left,
              top: b.top,
              right: b.right,
              bottom: b.bottom,
              width: b.width,
              height: b.height,
            };
          };
          const v = document.querySelector(".od-graph-viewport");
          return {
            viewport: {
              ...box(v),
              scrollLeft: v.scrollLeft,
              scrollWidth: v.scrollWidth,
              clientWidth: v.clientWidth,
            },
            canvas: box(document.querySelector(".od-graph-canvas")),
            child: box(
              document.querySelector('[data-service-api-name="new-child"]'),
            ),
            inspector: box(document.querySelector(".od-graph-inspector")),
            selected: document.querySelector(
              '[data-service-api-name][data-selected="true"]',
            )?.dataset.serviceApiName,
          };
        }),
        null,
        2,
      ),
    );
    await expect
      .poll(() =>
        node(page, "new-child").evaluate((e) => {
          const b = e.getBoundingClientRect(),
            panel = document
              .querySelector(".od-graph-inspector")
              .getBoundingClientRect(),
            viewport = document
              .querySelector(".od-graph-viewport")
              .getBoundingClientRect();
          return b.left >= viewport.left - 1 && b.right <= panel.left + 1;
        }),
      )
      .toBe(true);
  }
  await expect(inspector(page).getByRole("textbox")).toHaveCount(0);
  await expect(
    inspector(page).getByRole("button", {
      name: "Open service details",
      exact: true,
    }),
  ).toBeVisible();
  expect(await count(page, "workspaces")).toBe(0);
  expect(await count(page, "keys")).toBe(0);
}
const serverError = {
  status: 503,
  code: "unavailable",
  message: "Service creation failed. Try again.",
};
describe("contextual service creation in the real App", () => {
  for (const [width, height] of sizes) {
    it(
      `${width}: pointer/touch, real pending lock, errors, retry and compact success`,
      { timeout: 60000 },
      async () => {
        const { page, context, errors } = await open(width, height);
        try {
          await ready(page);
          await expect(node(page, "root")).toHaveAttribute(
            "aria-pressed",
            "true",
          );
          await expect(inspector(page)).toHaveCount(0);
          await placement(page);
          await evidenceFor(page, `${width}-normal`);
          if (width === 390) {
            await action(page).tap();
            await expect(heading(page)).toBeFocused();
            await page.keyboard.press("Tab");
            await expect(
              page.getByRole("textbox", { name: "Display name", exact: true }),
            ).toBeFocused();
          } else await draft(page);
          await evidenceFor(page, `${width}-create`);
          await start(page);
          const body = await page.evaluate(
            () =>
              window.creationFixture.calls.find(
                (c) => c.name === "createService",
              ).args,
          );
          expect(body).toEqual([
            {
              display_name: "New child",
              api_name: "new-child",
              parent_service_api_name: "root",
            },
            "synthetic-csrf",
          ]);
          for (let index = 0; index < 5; index++)
            await page.keyboard.press("Escape");
          await expect(heading(page)).toBeVisible();
          if (width === 390) {
            await expect(heading(page)).toBeFocused();
            for (let index = 0; index < 3; index++) {
              await page.keyboard.press("Tab");
              await expect(heading(page)).toBeFocused();
            }
            await node(page, "other").evaluate((e) => e.focus());
            await expect(heading(page)).toBeFocused();
          } else {
            await expect(node(page, "other")).toHaveAttribute(
              "aria-disabled",
              "true",
            );
            await node(page, "other").click({ force: true });
            await action(page).click({ force: true });
            await page
              .getByRole("link", { name: "Overview", exact: true })
              .click();
          }
          await inspector(page)
            .locator("form")
            .evaluate((e) => {
              e.requestSubmit();
              e.requestSubmit();
            });
          expect(await count(page, "createService")).toBe(1);
          expect(
            await page.evaluate(() => {
              const event = new Event("beforeunload", {
                cancelable: true,
              });
              window.dispatchEvent(event);
              return event.defaultPrevented;
            }),
          ).toBe(true);
          await expect(node(page, "root")).toHaveAttribute(
            "aria-pressed",
            "true",
          );
          expect(new URL(page.url()).pathname).toBe("/services");
          await evidenceFor(page, `${width}-pending`);
          await finish(page, "createService", undefined, serverError);
          await expect(submit(page)).toBeFocused();
          await expect(inspector(page).getByRole("alert")).toContainText(
            serverError.message,
          );
          await expect(
            page.getByRole("textbox", { name: "Display name", exact: true }),
          ).toHaveValue("New child");
          await evidenceFor(page, `${width}-server-error`);
          await submit(page).click();
          await pending(page, "createService");
          await finish(page, "createService", undefined, {
            status: 422,
            code: "validation_error",
            message: "Choose another API name.",
            details: { field: "api_name" },
          });
          await expect(
            page.getByRole("textbox", { name: "API name", exact: true }),
          ).toBeFocused();
          await evidenceFor(page, `${width}-field-error`);
          await submit(page).click();
          await success(page);
          await evidenceFor(page, `${width}-success`);
        } finally {
          await close(context, errors);
        }
      },
    );
    it(
      `${width}: cancel, repeated opener, keyboard entry, and 200 percent text`,
      { timeout: 45000 },
      async () => {
        const { page, context, errors } = await open(width, height);
        try {
          await ready(page);
          await node(page, "root").focus();
          await page.keyboard.press("ArrowDown");
          await expect(action(page)).toBeFocused();
          await page.keyboard.press("Enter");
          await expect(heading(page)).toBeFocused();
          await fill(page);
          if (width !== 390) {
            await action(page).click();
            await expect(heading(page)).toBeFocused();
            await expect(
              page.getByRole("textbox", { name: "Display name", exact: true }),
            ).toHaveValue("New child");
            await expect(inspector(page)).toHaveCount(1);
          }
          await page
            .getByRole("button", { name: "Close new service", exact: true })
            .click();
          await expect(action(page)).toBeFocused();
          await expect(inspector(page)).toHaveCount(0);
          await page.keyboard.press("Space");
          await expect(heading(page)).toBeFocused();
          await expect(
            page.getByRole("textbox", { name: "Display name", exact: true }),
          ).toHaveValue("");
          await expect(
            page.getByRole("textbox", { name: "API name", exact: true }),
          ).toHaveValue("");
          await page.keyboard.press("Escape");
          await expect(action(page)).toBeFocused();
          await page.keyboard.press("Tab");
          expect(
            await page.evaluate(
              () => !!document.activeElement.closest(".od-graph-canvas"),
            ),
          ).toBe(false);
          await expect(node(page, "root")).toHaveAttribute("tabindex", "0");
          await expect(action(page)).toHaveAttribute("tabindex", "-1");
          await page.evaluate(() => {
            document.documentElement.style.fontSize = "32px";
          });
          await placement(page);
          await action(page).click();
          await expect(heading(page)).toBeFocused();
          await fill(page);
          for (const control of await inspector(page)
            .locator("input, button")
            .all()) {
            await control.scrollIntoViewIfNeeded();
            await control.click({ trial: true });
          }
          await evidenceFor(page, `${width}-text200`);
          await page.keyboard.press("Escape");
          await expect(action(page)).toBeFocused();
          expect(await count(page, "createService")).toBe(0);
        } finally {
          await close(context, errors);
        }
      },
    );
  }
  it("exact contextual arrow order and Tab reset", async () => {
    const { page, context, errors } = await open(1440, 1000);
    try {
      await ready(page);
      await node(page, "root").focus();
      const names = await page
        .locator("[data-service-api-name]")
        .evaluateAll((nodes) => nodes.map((n) => n.dataset.serviceApiName));
      for (const key of ["ArrowUp", "ArrowLeft"]) {
        await page.keyboard.press("ArrowDown");
        await expect(action(page)).toBeFocused();
        await page.keyboard.press(key);
        await expect(node(page, "root")).toBeFocused();
      }
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowRight");
      await expect(action(page)).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(node(page, names[1])).toBeFocused();
      await page.keyboard.press("ArrowUp");
      await expect(action(page)).toBeFocused();
      await page.keyboard.press("End");
      await expect(node(page, names.at(-1))).toBeFocused();
      await page.keyboard.press("Home");
      await expect(node(page, "root")).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Home");
      await expect(node(page, "root")).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(node(page, "root")).toHaveAttribute("tabindex", "0");
      await expect(page.locator('.od-graph-canvas [tabindex="0"]')).toHaveCount(
        1,
      );
    } finally {
      await close(context, errors);
    }
  });
  for (const [width, height] of sizes.slice(0, 2))
    it(
      `${width}: empty selection, dirty confirmation and first entered field`,
      { timeout: 30000 },
      async () => {
        const { page, context, errors } = await open(width, height);
        try {
          await ready(page);
          await draft(page);
          await node(page, "parent").click();
          await expect(page.locator(".od-graph-inspector h2")).toHaveText(
            "Parent service",
          );
          await expect(page.locator(".od-graph-inspector h2")).toBeFocused();
          await page.keyboard.press("Escape");
          await node(page, "root").click();
          await expect(page.locator(".od-graph-inspector h2")).toHaveText(
            "Root service",
          );
          await expect(page.locator(".od-graph-inspector h2")).toBeFocused();
          await page.keyboard.press("Escape");
          await draft(page);
          for (const field of ["API name", "Display name"]) {
            await page
              .getByRole("textbox", { name: field, exact: true })
              .fill("entered-value");
            await node(page, "parent").focus();
            await page.keyboard.press("Space");
            await expect(
              page.getByRole("button", { name: "Keep editing", exact: true }),
            ).toBeFocused();
            await evidenceFor(
              page,
              `${width}-discard-${field === "API name" ? "api" : "display"}`,
            );
            if (field === "API name") await page.keyboard.press("Escape");
            else
              await page
                .getByRole("button", { name: "Keep editing", exact: true })
                .click();
            await expect(
              page.getByRole("textbox", { name: field, exact: true }),
            ).toBeFocused();
            await expect(node(page, "root")).toHaveAttribute(
              "aria-pressed",
              "true",
            );
          }
          await node(page, "parent").click();
          await page
            .getByRole("button", { name: "Discard values", exact: true })
            .click();
          await expect(page.locator(".od-graph-inspector h2")).toHaveText(
            "Parent service",
          );
          await expect(page.locator(".od-graph-inspector h2")).toBeFocused();
          expect(await count(page, "createService")).toBe(0);
        } finally {
          await close(context, errors);
        }
      },
    );
  it(
    "captured parent and current refresh authority across request races",
    { timeout: 60000 },
    async () => {
      for (const scenario of [
        "preopen",
        "late-success",
        "loss-before",
        "loss-success",
        "loss-failure",
        "newer-refresh",
        "partial",
        "parent-response",
      ]) {
        const { page, context, errors } = await open(1440, 1000);
        try {
          await ready(page);
          if (scenario === "preopen") {
            await hold(page, ["services", "createService"]);
            await refresh(page).click();
            await pending(page, "services");
          }
          await draft(page, "parent");
          if (scenario === "preopen") {
            await finish(page, "services", await list(page, true));
            await expect(heading(page)).toBeVisible();
            await expect(node(page, "parent")).toHaveAttribute(
              "aria-pressed",
              "true",
            );
          }
          if (scenario === "loss-before") {
            await hold(page, ["services", "createService"]);
            await refresh(page).click();
            await finish(page, "services", await list(page, true));
            await expect(inspector(page)).toHaveCount(0);
            await expect(node(page, "root")).toBeFocused();
            await expect(
              page.getByText(/The parent service is unavailable/),
            ).toBeVisible();
            await evidenceFor(page, "parent-unavailable-before");
            continue;
          }
          await start(page);
          expect(
            await page.evaluate(
              () =>
                window.creationFixture.calls.find(
                  (c) => c.name === "createService",
                ).args[0].parent_service_api_name,
            ),
          ).toBe("parent");
          if (
            [
              "late-success",
              "loss-success",
              "loss-failure",
              "newer-refresh",
              "partial",
            ].includes(scenario)
          ) {
            await hold(page, ["services", "createService"]);
            await refresh(page).click();
            await pending(page, "services");
            if (scenario !== "late-success") {
              await finish(
                page,
                "services",
                await list(page, true, scenario === "partial"),
              );
              await expect(heading(page)).toBeVisible();
              await expect(inspector(page)).toHaveAttribute(
                "aria-busy",
                "true",
              );
              if (scenario === "newer-refresh") {
                await refresh(page).click();
                await pending(page, "services");
                await finish(page, "services", await list(page));
              }
            }
          }
          if (
            [
              "loss-failure",
              "newer-refresh",
              "partial",
              "parent-response",
            ].includes(scenario)
          ) {
            await finish(
              page,
              "createService",
              undefined,
              scenario === "parent-response"
                ? {
                    status: 404,
                    code: "not_found",
                    message: "Parent service was not found.",
                  }
                : serverError,
            );
            if (["loss-failure", "parent-response"].includes(scenario)) {
              await expect(inspector(page)).toHaveCount(0);
              await expect(node(page, "root")).toBeFocused();
              await expect(node(page, "parent")).toHaveCount(0);
              await evidenceFor(page, scenario);
            } else {
              await expect(submit(page)).toBeFocused();
              await expect(node(page, "parent")).toHaveAttribute(
                "aria-pressed",
                "true",
              );
              await expect(
                page.getByRole("textbox", {
                  name: "Display name",
                  exact: true,
                }),
              ).toHaveValue("New child");
            }
          } else {
            await success(page);
            if (scenario === "late-success") {
              await finish(page, "services", await list(page, true));
              await expect(node(page, "new-child")).toBeVisible();
            }
            await expect(inspector(page).locator("dd").nth(1)).toContainText(
              "Parent service parent",
            );
          }
        } finally {
          await close(context, errors);
        }
      }
    },
  );
  for (const [width, height] of sizes)
    it(
      `${width}: loading, failed bootstrap, invalid lists, retry and missing URL`,
      { timeout: 45000 },
      async () => {
        for (const state of ["error", "empty", "root-missing", "missing-url"]) {
          const { page, context, errors } = await open(width, height, {
            hold: ["services", "createService"],
            path: "/services?service=absent",
          });
          try {
            await pending(page, "services");
            await expect(
              page.getByRole("button", { name: /^New service under/ }),
            ).toHaveCount(0);
            if (state === "error") {
              await evidenceFor(page, `${width}-loading`);
              await finish(page, "services", undefined, serverError);
            } else if (state === "empty")
              await finish(page, "services", {
                items: [],
                page: { has_more: false },
              });
            else if (state === "root-missing")
              await finish(page, "services", {
                items: (await list(page)).items.filter(
                  (s) => s.api_name !== "root",
                ),
                page: { has_more: false },
              });
            else await finish(page, "services");
            if (state !== "missing-url") {
              await expect(
                page.getByRole("button", {
                  name: "Retry services",
                  exact: true,
                }),
              ).toBeVisible();
              await expect(
                page.getByRole("button", { name: /^New service under/ }),
              ).toHaveCount(0);
              await evidenceFor(page, `${width}-${state}`);
              await page
                .getByRole("button", { name: "Retry services", exact: true })
                .click();
              await pending(page, "services");
              await finish(page, "services");
            }
            await ready(page);
            await expect(node(page, "root")).toHaveAttribute(
              "aria-pressed",
              "true",
            );
            await placement(page);
          } finally {
            await close(context, errors);
          }
        }
      },
    );
  it(
    "pending write blocks real Back and Forward without resuming navigation",
    { timeout: 30000 },
    async () => {
      const { page, context, errors } = await open(1440, 1000, {
        path: "/overview",
      });
      try {
        await page.getByRole("link", { name: "Services", exact: true }).click();
        await ready(page);
        await page.getByRole("link", { name: "Overview", exact: true }).click();
        await page.goBack();
        await ready(page);
        await draft(page);
        await start(page);
        for (const direction of ["back", "forward"]) {
          await page.evaluate(
            (direction) => window.history[direction](),
            direction,
          );
          await expect
            .poll(() => new URL(page.url()).pathname)
            .toBe("/services");
          await expect(heading(page)).toBeVisible();
        }
        await success(page);
        expect(new URL(page.url()).pathname).toBe("/services");
        expect(await count(page, "createService")).toBe(1);
      } finally {
        await close(context, errors);
      }
    },
  );
});

describe("creation boundary checks", () => {
  it("browser validation keeps focus and sends no write", async () => {
    const { page, context, errors } = await open(1440, 1000);
    try {
      await ready(page);
      await draft(page);
      await submit(page).click();
      await expect(
        page.getByRole("textbox", { name: "Display name", exact: true }),
      ).toBeFocused();
      await page
        .getByRole("textbox", { name: "Display name", exact: true })
        .fill("New child");
      await page
        .getByRole("textbox", { name: "API name", exact: true })
        .fill("INVALID NAME");
      await submit(page).click();
      await expect(
        page.getByRole("textbox", { name: "API name", exact: true }),
      ).toBeFocused();
      expect(await count(page, "createService")).toBe(0);
    } finally {
      await close(context, errors);
    }
  });
  it("phone sheet blocks background pointer and keyboard selection", async () => {
    const { page, context, errors } = await open(390, 844);
    try {
      await ready(page);
      await node(page, "parent").tap();
      await expect(page.locator(".od-graph-inspector h2")).toHaveText(
        "Parent service",
      );
      await expect(page.locator(".od-graph-inspector h2")).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(node(page, "parent")).toBeFocused();
      await action(page, "Parent service", "parent").tap();
      await expect(heading(page)).toBeFocused();
      await expect(inspector(page).locator("dd")).toContainText(
        "Parent service parent",
      );
      const background = await node(page, "root").boundingBox();
      await page.touchscreen.tap(
        background.x + background.width / 2,
        background.y + background.height / 2,
      );
      await expect(heading(page)).toBeVisible();
      await expect(node(page, "parent")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      for (let index = 0; index < 10; index++) {
        await page.keyboard.press("Tab");
        expect(
          await page.evaluate(
            () => !!document.activeElement.closest(".od-graph-inspector"),
          ),
        ).toBe(true);
      }
      const navigation = page.getByRole("button", {
        name: "Navigation",
        exact: true,
      });
      await navigation.evaluate((e) => e.focus());
      expect(
        await page.evaluate(
          () => !!document.activeElement.closest(".od-graph-inspector"),
        ),
      ).toBe(true);
      await start(page);
      expect(
        await page.evaluate(
          () =>
            window.creationFixture.calls.find((c) => c.name === "createService")
              .args[0].parent_service_api_name,
        ),
      ).toBe("parent");
      await success(page);
    } finally {
      await close(context, errors);
    }
  });
  it(
    "actual reload confirmation preserves the pending write when dismissed",
    { timeout: 15000 },
    async () => {
      const { page, context, errors } = await open(1440, 1000);
      try {
        await ready(page);
        await draft(page);
        await start(page);
        const dialogSeen = page.waitForEvent("dialog");
        const reload = page.reload().then(
          () => "loaded",
          () => "cancelled",
        );
        const dialog = await dialogSeen;
        expect(dialog.type()).toBe("beforeunload");
        await dialog.dismiss();
        expect(await reload).toBe("cancelled");
        await expect(heading(page)).toBeVisible();
        expect(await count(page, "createService")).toBe(1);
        await success(page);
      } finally {
        await close(context, errors);
      }
    },
  );
  it("partial retrieval cannot remove a parent, and stale failure keeps the draft", async () => {
    const { page, context, errors } = await open(1440, 1000);
    try {
      await ready(page);
      await draft(page, "parent");
      await fill(page);
      await hold(page, ["services", "createService"]);
      await refresh(page).click();
      await pending(page, "services");
      const partial = await list(page, true);
      partial.retrieval = { complete: false, reason: "limit" };
      await finish(page, "services", partial);
      await expect(heading(page)).toBeVisible();
      await expect(node(page, "parent")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await refresh(page).click();
      await pending(page, "services");
      await finish(page, "services", undefined, serverError);
      await expect(heading(page)).toBeVisible();
      await expect(
        page.getByRole("textbox", { name: "Display name", exact: true }),
      ).toHaveValue("New child");
      await evidenceFor(page, "stale-refresh-draft");
    } finally {
      await close(context, errors);
    }
  });
  it("pre-open service result cannot replace a later confirmed child", async () => {
    const { page, context, errors } = await open(1440, 1000);
    try {
      await ready(page);
      await hold(page, ["services", "createService"]);
      await refresh(page).click();
      await pending(page, "services");
      await draft(page, "parent");
      await start(page);
      await success(page);
      await finish(page, "services", await list(page, true));
      await expect(node(page, "new-child")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await expect(page.locator(".od-graph-inspector h2")).toHaveText(
        "New child",
      );
    } finally {
      await close(context, errors);
    }
  });
  for (const [width, height] of sizes)
    it(
      `${width}: oversized graph uses local scrolling at both text sizes`,
      { timeout: 30000 },
      async () => {
        const services = [
          {
            api_name: "root",
            display_name: "Root service",
            parent_service_api_name: null,
            created_at: "2026-08-25T00:00:00Z",
          },
        ];
        const keys = Array.from(
          { length: 24 },
          (_, index) =>
            `service-${index}${index % 2 ? "-" + "a".repeat(45) : ""}`,
        );
        for (let index = 0; index < 24; index++)
          services.push({
            api_name: keys[index],
            display_name: `Service ${index} with a long display name`,
            parent_service_api_name:
              index < 12 ? "root" : index === 12 ? keys[0] : keys[index - 1],
            created_at: "2026-08-25T00:00:00Z",
          });
        const { page, context, errors } = await open(width, height, {
          services,
        });
        try {
          await ready(page);
          for (const size of [16, 32]) {
            await page.evaluate((size) => {
              document.documentElement.style.fontSize = `${size}px`;
            }, size);
            await placement(page);
            const endpoints = await page
              .locator(".od-graph-edge-line")
              .evaluateAll((paths) =>
                paths.map((path) => {
                  const transform = path.getScreenCTM();
                  const start = path
                    .getPointAtLength(0)
                    .matrixTransform(transform);
                  const end = path
                    .getPointAtLength(path.getTotalLength())
                    .matrixTransform(transform);
                  const boxes = [
                    ...document.querySelectorAll("[data-service-api-name]"),
                  ].map((node) => node.getBoundingClientRect());
                  return {
                    start: boxes.some(
                      (b) =>
                        Math.abs(start.x - (b.left + b.width / 2)) <= 1 &&
                        Math.abs(start.y - b.bottom) <= 1,
                    ),
                    end: boxes.some(
                      (b) =>
                        Math.abs(end.x - (b.left + b.width / 2)) <= 1 &&
                        Math.abs(end.y - b.top) <= 1,
                    ),
                  };
                }),
              );
            expect(endpoints).toHaveLength(24);
            for (const endpoint of endpoints)
              expect(endpoint).toEqual({ start: true, end: true });
            const viewport = page.locator(".od-graph-viewport"),
              toolbar = await refresh(page).boundingBox();
            const local = await viewport.evaluate((e) => {
              e.scrollLeft = e.scrollWidth;
              e.scrollTop = e.scrollHeight;
              return {
                x: e.scrollLeft,
                y: e.scrollTop,
                maxX: e.scrollWidth - e.clientWidth,
                maxY: e.scrollHeight - e.clientHeight,
              };
            });
            expect(local.x).toBeGreaterThan(0);
            expect(local.y).toBeGreaterThan(0);
            expect(await refresh(page).boundingBox()).toEqual(toolbar);
            await node(page, keys.at(-1)).scrollIntoViewIfNeeded();
            await expect(node(page, keys.at(-1))).toBeInViewport();
            await evidenceFor(
              page,
              `${width}-oversized-${size === 16 ? "100" : "200"}`,
            );
            await node(page, "root").focus();
            await page.keyboard.press("ArrowDown");
            await expect(action(page)).toBeFocused();
            await page.keyboard.press("Space");
            await expect(heading(page)).toBeFocused();
            for (const control of await inspector(page)
              .locator("input, button")
              .all()) {
              await control.scrollIntoViewIfNeeded();
              await control.click({ trial: true });
            }
            await evidenceFor(
              page,
              `${width}-oversized-create-${size === 16 ? "100" : "200"}`,
            );
            await page.keyboard.press("Escape");
            await expect(action(page)).toBeFocused();
          }
        } finally {
          await close(context, errors);
        }
      },
    );
});

describe("dirty service navigation", () => {
  it(
    "node Enter cancels or accepts one discard confirmation",
    { timeout: 20000 },
    async () => {
      const { page, context, errors } = await open(1440, 1000);
      let confirmationCount = 0,
        accept = false;
      page.on("dialog", async (dialog) => {
        confirmationCount++;
        expect(dialog.type()).toBe("confirm");
        if (accept) await dialog.accept();
        else await dialog.dismiss();
      });
      try {
        await ready(page);
        await draft(page);
        await fill(page);
        await node(page, "parent").focus();
        await page.keyboard.press("Enter");
        await expect.poll(() => confirmationCount).toBe(1);
        await expect(heading(page)).toBeVisible();
        await expect(
          page.getByRole("textbox", { name: "Display name", exact: true }),
        ).toHaveValue("New child");
        expect(new URL(page.url()).pathname).toBe("/services");
        accept = true;
        await node(page, "parent").focus();
        await page.keyboard.press("Enter");
        await expect(page.locator("main h1")).toHaveText("Parent service");
        expect(confirmationCount).toBe(2);
        expect(new URL(page.url()).pathname).toBe("/services/parent");
        await page.goBack();
        await expect(page.locator("main h1")).toHaveText("Services");
        await expect(heading(page)).toHaveCount(0);
        expect(confirmationCount).toBe(2);
        expect(await count(page, "createService")).toBe(0);
      } finally {
        await close(context, errors);
      }
    },
  );
});

describe("independent creation review", () => {
  it("pre-open reads cannot change a pending draft or a failed attempt", async () => {
    for (const timing of ["pending", "failed"]) {
      const { page, context, errors } = await open(1440, 1000);
      try {
        await ready(page);
        await hold(page, ["services", "createService"]);
        await refresh(page).click();
        await pending(page, "services");
        await draft(page, "parent");
        await start(page);
        if (timing === "pending") {
          await finish(page, "services", await list(page, true));
          await expect(inspector(page)).toHaveAttribute("aria-busy", "true");
        }
        await finish(page, "createService", undefined, serverError);
        if (timing === "failed")
          await finish(page, "services", await list(page, true));
        await expect(submit(page)).toBeFocused();
        await expect(node(page, "parent")).toHaveAttribute(
          "aria-pressed",
          "true",
        );
        await expect(
          page.getByRole("textbox", { name: "API name", exact: true }),
        ).toHaveValue("new-child");
        await expect(inspector(page).locator("dd")).toHaveText(
          "Parent service parent",
        );
        expect(await count(page, "createService")).toBe(1);
      } finally {
        await close(context, errors);
      }
    }
  });

  it("current parent changes keep the captured context and use the confirmed child", async () => {
    const { page, context, errors } = await open(1440, 1000);
    try {
      await ready(page);
      await draft(page, "parent");
      await fill(page);
      await hold(page, ["services", "createService"]);
      const reads = await count(page, "services");
      await refresh(page).evaluate((button) => {
        button.click();
        button.click();
      });
      await pending(page, "services");
      expect(await count(page, "services")).toBe(reads + 1);
      const current = await list(page);
      current.items.find(
        (service) => service.api_name === "parent",
      ).display_name = "Renamed parent";
      await finish(page, "services", current);
      await expect(node(page, "parent")).toContainText("Renamed parent");
      await expect(inspector(page).locator("dd")).toHaveText(
        "Parent service parent",
      );
      await action(page, "Renamed parent", "parent").click();
      await expect(heading(page)).toBeFocused();
      await expect(
        page.getByRole("textbox", { name: "API name", exact: true }),
      ).toHaveValue("new-child");
      await submit(page).evaluate((button) => {
        button.click();
        button.click();
      });
      await pending(page, "createService");
      expect(await count(page, "createService")).toBe(1);
      await finish(page, "createService", {
        api_name: "new-child",
        display_name: "Confirmed child",
        parent_service_api_name: "parent",
        created_at: "2026-09-09T00:00:00Z",
      });
      await expect(inspector(page).locator("h2")).toHaveText("Confirmed child");
      await expect(inspector(page).locator("h2")).toBeFocused();
      await expect(node(page, "new-child")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await expect(inspector(page).locator("dd").nth(1)).toHaveText(
        "Renamed parent parent",
      );
      expect(new URL(page.url()).pathname).toBe("/services");
      await evidenceFor(page, "review-confirmed-child");
    } finally {
      await close(context, errors);
    }
  });

  it("current parent loss closes an open discard confirmation and restores node focus", async () => {
    const { page, context, errors } = await open(1440, 1000);
    try {
      await ready(page);
      await draft(page, "parent");
      await fill(page);
      await hold(page, ["services", "createService"]);
      await refresh(page).click();
      await pending(page, "services");
      await node(page, "other").click();
      await expect(
        page.getByRole("button", { name: "Keep editing", exact: true }),
      ).toBeFocused();
      await finish(page, "services", await list(page, true));
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await expect(node(page, "root")).toBeFocused();
      await expect(node(page, "root")).toHaveAttribute("aria-pressed", "true");
      await expect(action(page)).toBeVisible();
      expect(await count(page, "createService")).toBe(0);
      await evidenceFor(page, "review-loss-during-discard");
    } finally {
      await close(context, errors);
    }
  });

  it("dirty browser Back can be canceled and then accepted once", async () => {
    const { page, context, errors } = await open(1440, 1000, {
      path: "/overview",
    });
    let accept = false,
      confirmations = 0;
    page.on("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      confirmations++;
      if (accept) await dialog.accept();
      else await dialog.dismiss();
    });
    try {
      await page.getByRole("link", { name: "Services", exact: true }).click();
      await ready(page);
      await draft(page, "parent");
      const apiName = page.getByRole("textbox", {
        name: "API name",
        exact: true,
      });
      await apiName.fill("review-draft");
      await page.evaluate(() => window.history.back());
      await expect.poll(() => confirmations).toBe(1);
      await expect.poll(() => new URL(page.url()).pathname).toBe("/services");
      await expect(apiName).toBeFocused();
      await expect(apiName).toHaveValue("review-draft");
      expect(
        JSON.stringify(await page.evaluate(() => window.history.state)),
      ).not.toContain("review-draft");
      accept = true;
      await page.evaluate(() => window.history.back());
      await expect(page.locator("main h1")).toHaveText("Overview");
      expect(confirmations).toBe(2);
      await page.goForward();
      await expect(page.locator("main h1")).toHaveText("Services");
      await expect(heading(page)).toHaveCount(0);
      expect(await count(page, "createService")).toBe(0);
    } finally {
      await close(context, errors);
    }
  });
});
