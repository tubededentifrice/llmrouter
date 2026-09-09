// Run: node apps/admin/test/service-details.browser.mjs
/* global window, document, innerWidth, history, navigator, location, localStorage, sessionStorage, getComputedStyle */
// The real App uses controlled client calls. No server session or provider is used.
import { existsSync } from "node:fs";
import { mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { URL } from "node:url";
import { after, before, describe, it as nodeTest } from "node:test";
function beforeAll(callback, timeout) {
  before(callback, { timeout });
}
function afterAll(callback) {
  after(callback);
}
function it(name, callback, timeout) {
  nodeTest(name, { timeout }, callback);
}
const root = resolve(import.meta.dirname, "../../..");
const requireShared = createRequire(
  resolve(root, "../opendle-ui/package.json"),
);
const { chromium, expect: browserExpect } = requireShared("@playwright/test");
const expect = browserExpect;
const { build } = requireShared("esbuild");
const { default: AxeBuilder } = requireShared("@axe-core/playwright");
const shared = dirname(
  createRequire(import.meta.url).resolve("@opendle/ui/package.json"),
);
const evidence = resolve(
  process.env.LLMROUTER_DETAILS_EVIDENCE ?? "/tmp/llmrouter-details-browser",
);
let browser;
let script = "";
let css = "";
beforeAll(async () => {
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
  script = bundle.outputFiles[0]?.text ?? "";
  expect(script.length).toBeGreaterThan(0);
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
}, 30_000);
afterAll(async () => {
  await browser?.close();
});
async function open(width, height, boot = {}) {
  const context = await browser.newContext({
    viewport: { width, height },
    hasTouch: width === 390,
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    expect(url.origin).toBe("http://127.0.0.1:5174");
    if (url.pathname === "/shell-fixture.js")
      await route.fulfill({ contentType: "text/javascript", body: script });
    else if (url.pathname === "/shell-fixture.css")
      await route.fulfill({ contentType: "text/css", body: css });
    else
      await route.fulfill({
        contentType: "text/html",
        body: `<!doctype html><html lang="en"><head><title>Shell fixture</title><link rel="stylesheet" href="/shell-fixture.css"></head><body><div id="root"></div><script>window.shellBoot=${JSON.stringify({ hold: [], fail: [], signedOut: false, ...boot })}</script><script src="/shell-fixture.js"></script></body></html>`,
      });
  });
  return { context, page, errors };
}
async function visit(page, path) {
  await page.goto(`http://127.0.0.1:5174${path}`);
  await browserExpect(page.locator(".od-application-shell")).toBeVisible();
  await browserExpect(page.locator("main h1")).toHaveCount(1);
}
async function settle(page) {
  await browserExpect(page.locator('[aria-busy="true"]')).toHaveCount(0);
}
async function navigate(page, label, phone) {
  if (phone) {
    await page.getByRole("button", { name: "Navigation", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await browserExpect(
      dialog.getByRole("link", { name: "Overview", exact: true }),
    ).toBeFocused();
    await dialog.getByRole("link", { name: label, exact: true }).click();
    await browserExpect(dialog).toHaveCount(0);
  } else
    await page
      .locator(".od-application-sidebar")
      .getByRole("link", { name: label, exact: true })
      .click();
  await browserExpect(page.locator("main h1")).toBeFocused();
}
async function shellGeometry(page, phone, height) {
  await browserExpect(page.locator(".od-application-topbar")).toHaveCount(0);
  await browserExpect(
    page.getByRole("combobox", { name: "Selected service", exact: true }),
  ).toHaveCount(0);
  await browserExpect(
    page.getByRole("button", { name: "Refresh", exact: true }),
  ).toHaveCount(0);
  const geometry = await page.evaluate(() => {
    const box = (selector) =>
      document.querySelector(selector)?.getBoundingClientRect();
    const main = box(".od-application-main");
    const sidebar = box(".od-application-sidebar");
    const bottom = box(".od-application-mobile-navigation");
    const column = document.querySelector(".od-application-column");
    return {
      mainLeft: main?.left,
      mainTop: main?.top,
      sidebarRight: sidebar?.right,
      sidebarTop: sidebar?.top,
      sidebarBottom: sidebar?.bottom,
      bottomTop: bottom?.top,
      bottomBottom: bottom?.bottom,
      firstChild: column?.firstElementChild?.tagName,
      overflow: document.documentElement.scrollWidth > innerWidth,
    };
  });
  expect(geometry.mainTop).toBeCloseTo(0, 0);
  expect(geometry.firstChild).toBe("MAIN");
  expect(geometry.overflow).toBe(false);
  if (phone) {
    expect(geometry.mainLeft).toBeCloseTo(0, 0);
    expect(geometry.bottomBottom).toBeCloseTo(height, 0);
    await browserExpect(page.locator(".od-application-sidebar")).toBeHidden();
  } else {
    expect(geometry.sidebarTop).toBeCloseTo(0, 0);
    expect(geometry.sidebarBottom).toBeCloseTo(height, 0);
    expect(geometry.sidebarRight).toBeCloseTo(geometry.mainLeft ?? -1, 0);
  }
}
async function blockedHistory(page, direction) {
  // Wait for both the attempted traversal and the guard's return traversal.
  // A URL assertion alone can pass before the first popstate event arrives.
  await page.evaluate(
    (value) =>
      new Promise((resolve) => {
        let events = 0;
        const restored = () => {
          events += 1;
          if (events !== 2) return;
          window.removeEventListener("popstate", restored);
          resolve();
        };
        window.addEventListener("popstate", restored);
        history[value]();
      }),
    direction,
  );
}
async function dismissUnload(page, action) {
  const question = page.waitForEvent("dialog");
  const attempt =
    action === "reload"
      ? page.evaluate(() => location.reload())
      : page.close({ runBeforeUnload: true });
  const dialog = await question;
  expect(dialog.type()).toBe("beforeunload");
  await dialog.dismiss();
  await attempt;
  expect(page.isClosed()).toBe(false);
  await browserExpect(page).toHaveURL("http://127.0.0.1:5174/services/child");
}
function record(scope, name) {
  return scope
    .locator("td, dd")
    .filter({ hasText: new RegExp(`^${name}$`) })
    .filter({ visible: true });
}
async function screenshot(page, name) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const accessibility = await new AxeBuilder({ page }).analyze();
  await page.screenshot({
    path: resolve(evidence, `${page.viewportSize().width}-${name}.png`),
    fullPage: true,
  });
  expect(accessibility.violations).toEqual([]);
}
async function calls(page) {
  return page.evaluate(() =>
    window.shellFixture.calls.map((call) => call.name),
  );
}
async function clearCalls(page) {
  await page.evaluate(() => {
    window.shellFixture.calls.length = 0;
  });
}
async function changeFixture(page, patch) {
  await page.evaluate((value) => {
    Object.assign(window.shellFixture, value);
  }, patch);
}
async function close(context, errors) {
  await context.close();
  expect(errors).toEqual([]);
}
async function patchValues(page, values) {
  await page.evaluate(
    (value) => Object.assign(window.shellFixture.values, value),
    values,
  );
}
async function finish(page, name, fail = false) {
  await page.evaluate(
    ({ name, fail }) => window.shellFixture.finish(name, fail),
    { name, fail },
  );
}
const metadata = { created_at: "2026-08-25T00:00:00Z" };
const child = {
  ...metadata,
  api_name: "child",
  display_name: "Child service",
  parent_service_api_name: "root",
};
const rootService = {
  ...metadata,
  api_name: "root",
  display_name: "Root service",
  parent_service_api_name: null,
};
// Reviewed content inventory: present only in its named state.
const keep = [
  ["The permanent root service cannot be deleted.", "safety"],
  [
    "This action deletes the service, API keys, workspaces, local assignments, logs, raw accounting, daily aggregates, media jobs, and retained media. It keeps parent and child services.",
    "destructive impact",
  ],
  ["Accounting labels for this service.", "non-obvious effect"],
  [
    "Backend-only bearer credentials with full service authority.",
    "security boundary",
  ],
  [
    "The Router will not show it again. Deploy it to the service backend, and then clear this value.",
    "required action",
  ],
];
describe("service details and compact graph", () => {
  for (const [width, height, textSize] of [
    [1440, 1000, 100],
    [1100, 800, 100],
    [390, 844, 100],
    [390, 844, 200],
  ]) {
    it(`keeps details, compact navigation and content at ${width}/${textSize}`, async () => {
      const { context, page, errors } = await open(width, height);
      try {
        await visit(page, "/services?service=child");
        await settle(page);
        if (textSize === 200)
          await page.addStyleTag({ content: "html {font-size:200%;}" });
        await browserExpect(
          page.getByRole("button", {
            name: "Open service details",
            exact: true,
          }),
        ).toBeVisible();
        expect(
          (await calls(page)).filter((name) =>
            ["keys", "workspaces"].includes(name),
          ),
        ).toEqual([]);
        await screenshot(page, `${width}-${textSize}-compact`);
        await page.keyboard.press("Escape");
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services?service=child",
        );
        const node = page.locator('[data-service-api-name="child"]');
        await browserExpect(node).toBeFocused();
        await node.press("Space");
        await browserExpect(
          page.getByRole("button", {
            name: "Open service details",
            exact: true,
          }),
        ).toBeVisible();
        const length = await page.evaluate(() => history.length);
        await page
          .getByRole("button", { name: "Open service details", exact: true })
          .click();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child",
        );
        await browserExpect(page.locator("main h1")).toHaveText(
          "Child service",
        );
        await settle(page);
        expect(await page.evaluate(() => history.length)).toBe(length + 1);
        await browserExpect(page.locator("main h1")).toBeFocused();
        await page.keyboard.press("Tab");
        await browserExpect(
          page.getByRole("button", { name: "Back to services", exact: true }),
        ).toBeFocused();
        await browserExpect(
          page.getByRole("combobox", { name: "Parent service", exact: true }),
        ).toBeVisible();
        for (const label of [
          "Service facts",
          "Workspaces for Child service",
          "Service API keys for Child service",
        ])
          await browserExpect(
            page.getByRole("region", { name: label, exact: true }),
          ).toBeVisible();
        await browserExpect(
          page.getByText(keep[1][0], { exact: true }),
        ).toBeVisible();
        await browserExpect(
          page.getByText(keep[0][0], { exact: true }),
        ).toHaveCount(0);
        await shellGeometry(page, width === 390, height);
        await screenshot(page, `${width}-${textSize}-details`);
        await page.goBack();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services?service=child",
        );
        await settle(page);
        await browserExpect(
          page.getByRole("button", {
            name: "Open service details",
            exact: true,
          }),
        ).toBeVisible();
        if (width === 390)
          await browserExpect(
            page.locator(".od-graph-inspector h2"),
          ).toBeFocused();
        else await browserExpect(node).toBeFocused();
        await page.goForward();
        await browserExpect(page.locator("main h1")).toHaveText(
          "Child service",
        );
        await browserExpect(page.locator("main h1")).toBeFocused();
        await page
          .getByRole("button", { name: "Back to services", exact: true })
          .click();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services?service=child",
        );
        await page.keyboard.press("Escape");
        await node.press("Enter");
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child",
        );
        await visit(page, "/services/root");
        await settle(page);
        await browserExpect(
          page.getByText(keep[0][0], { exact: true }),
        ).toBeVisible();
        await browserExpect(
          page.getByRole("button", { name: "Delete service", exact: true }),
        ).toHaveCount(0);
        await browserExpect(
          page.getByRole("combobox", { name: "Parent service", exact: true }),
        ).toHaveCount(0);
        await screenshot(page, `${width}-${textSize}-root`);
        await page
          .getByRole("button", { name: "Back to services", exact: true })
          .click();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services?service=root",
        );
        await settle(page);
        await browserExpect(
          page.locator(".od-graph-inspector h2"),
        ).toBeFocused();
      } finally {
        await close(context, errors);
      }
    }, 45000);
  }
  for (const [width, height] of [
    [1440, 1000],
    [1100, 800],
    [390, 844],
  ]) {
    it(`binds direct encoded routes, errors, stale sections, and pending navigation at ${width}`, async () => {
      const { context, page, errors } = await open(width, height, {
        hold: ["service"],
      });
      try {
        await visit(page, "/services/%63hild");
        await browserExpect(
          page.getByText("Loading service details.", { exact: true }),
        ).toBeVisible();
        expect(await calls(page)).toEqual(["session", "service"]);
        await screenshot(page, "details-loading");
        await finish(page, "service", true);
        await browserExpect(
          page.getByText("Service details are unavailable.", { exact: true }),
        ).toBeVisible();
        await screenshot(page, "details-failed");
        await page
          .getByRole("button", { name: "Try again", exact: true })
          .click();
        await finish(page, "service");
        await settle(page);
        await page
          .getByRole("textbox", { name: "Display name", exact: true })
          .fill("Unsaved child");
        page.once("dialog", (dialog) => dialog.dismiss());
        await navigate(page, "Overview", width === 390);
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/%63hild",
        );
        page.once("dialog", (dialog) => dialog.dismiss());
        await page.evaluate(() => history.back());
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/%63hild",
        );
        await changeFixture(page, { hold: ["updateService"], fail: [] });
        await page
          .getByRole("button", { name: "Save changes", exact: true })
          .click();
        await clearCalls(page);
        await page
          .getByRole("button", { name: "Saving changes…", exact: true })
          .evaluate((button) => button.click());
        expect(await calls(page)).toEqual([]);
        await navigate(page, "Overview", width === 390);
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/%63hild",
        );
        await finish(page, "updateService", true);
        await browserExpect(
          page.getByRole("textbox", { name: "Display name", exact: true }),
        ).toHaveValue("Unsaved child");
        await screenshot(page, "details-save-failed");
        await patchValues(page, {
          updateService: { ...child, display_name: "Saved child" },
        });
        await page
          .getByRole("button", { name: "Save changes", exact: true })
          .click();
        await finish(page, "updateService");
        await settle(page);
        await browserExpect(page.locator("main h1")).toHaveText("Saved child");
        await changeFixture(page, { hold: [], fail: ["services"] });
        await page
          .getByRole("button", { name: "Refresh service", exact: true })
          .click();
        await browserExpect(
          page.getByText("Parent options are unavailable.", { exact: true }),
        ).toBeVisible();
        await browserExpect(
          page.getByRole("button", { name: "Create workspace", exact: true }),
        ).toBeEnabled();
        await browserExpect(
          page.getByRole("button", { name: "Save changes", exact: true }),
        ).toBeDisabled();
        await screenshot(page, "parent-options-failed");
        await changeFixture(page, { hold: [], fail: [] });
        await page
          .getByRole("button", { name: "Retry parent options", exact: true })
          .click();
        await browserExpect(
          page.getByRole("button", { name: "Save changes", exact: true }),
        ).toBeEnabled();
        await changeFixture(page, { hold: [], fail: ["service"] });
        await page
          .getByRole("button", { name: "Refresh service", exact: true })
          .click();
        await browserExpect(
          page.getByText("Service details are stale.", { exact: true }),
        ).toBeVisible();
        await browserExpect(
          page.getByRole("button", { name: "Create workspace", exact: true }),
        ).toBeDisabled();
        await browserExpect(
          page.getByRole("button", { name: "Create key", exact: true }),
        ).toBeDisabled();
        await screenshot(page, "service-stale");
        await visit(page, "/services/missing");
        await browserExpect(
          page.getByText("The service is unavailable.", { exact: true }),
        ).toBeVisible();
        await screenshot(page, "service-absent");
        await page
          .getByRole("button", { name: "Back to services", exact: true })
          .click();
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/services");
      } finally {
        await close(context, errors);
      }
    }, 45000);
    it(`excludes descendants and changes route only after confirmed deletion at ${width}`, async () => {
      const { context, page, errors } = await open(width, height);
      try {
        await visit(page, "/services/child");
        await settle(page);
        await patchValues(page, {
          services: {
            items: [
              rootService,
              child,
              {
                ...metadata,
                api_name: "grandchild",
                display_name: "Grandchild",
                parent_service_api_name: "child",
              },
            ],
            page: { has_more: false },
          },
        });
        await page
          .getByRole("button", { name: "Refresh service", exact: true })
          .click();
        await settle(page);
        await browserExpect(
          page.getByRole("button", { name: "Delete service", exact: true }),
        ).toBeDisabled();
        await page
          .getByRole("combobox", { name: "Parent service", exact: true })
          .click();
        await browserExpect(
          page.getByRole("option", { name: /Grandchild/ }),
        ).toHaveCount(0);
        await browserExpect(
          page.getByRole("option", { name: /Child service/ }),
        ).toHaveCount(0);
        await page.keyboard.press("Escape");
        const childCount = page
          .locator(".od-graph-inspector-fact")
          .filter({
            has: page.locator("dt", { hasText: /^Direct children$/ }),
          })
          .locator("dd");
        await browserExpect(childCount).toHaveText("1");
        await changeFixture(page, { hold: ["services"], fail: ["services"] });
        await page
          .getByRole("button", { name: "Refresh service", exact: true })
          .click();
        await browserExpect(childCount).toHaveText("1 (refreshing)");
        await finish(page, "services", true);
        await browserExpect(childCount).toHaveText("1 (stale)");
        await browserExpect(
          page.getByRole("button", { name: "Save changes", exact: true }),
        ).toBeDisabled();
        await browserExpect(
          page.getByRole("button", { name: "Delete service", exact: true }),
        ).toBeDisabled();
        await browserExpect(
          page.getByRole("button", { name: "Create workspace", exact: true }),
        ).toBeEnabled();
        await browserExpect(
          page.getByRole("button", { name: "Create key", exact: true }),
        ).toBeEnabled();
        await screenshot(page, "child-count-stale");
        await changeFixture(page, { hold: [], fail: [] });
        await patchValues(page, {
          services: { items: [rootService, child], page: { has_more: false } },
        });
        await page
          .getByRole("button", { name: "Refresh service", exact: true })
          .click();
        await settle(page);
        const action = page.getByRole("button", {
          name: "Delete service",
          exact: true,
        });
        await browserExpect(childCount).toHaveText("0");
        await action.click();
        const dialog = page.getByRole("dialog");
        await dialog
          .getByRole("button", { name: "Cancel", exact: true })
          .click();
        await browserExpect(action).toBeFocused();
        await action.click();
        await dialog
          .getByRole("textbox")
          .fill("Service child will be deleted.");
        await changeFixture(page, { hold: ["deleteService"] });
        await dialog
          .getByRole("button", { name: "Delete service", exact: true })
          .click();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child",
        );
        await finish(page, "deleteService", true);
        await browserExpect(
          dialog.getByText("The service was not deleted.", { exact: true }),
        ).toBeVisible();
        await screenshot(page, "delete-failed");
        await dialog
          .getByRole("button", { name: "Delete service", exact: true })
          .click();
        await patchValues(page, {
          services: { items: [rootService], page: { has_more: false } },
        });
        await finish(page, "deleteService");
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/services");
        await settle(page);
        await browserExpect(
          page.locator('[data-service-api-name="root"]'),
        ).toBeFocused();
      } finally {
        await close(context, errors);
      }
    }, 30000);
    it(`keeps a secret when a service disappears and blocks history until clear at ${width}`, async () => {
      const { context, page, errors } = await open(width, height);
      try {
        await visit(page, "/services?service=child");
        await settle(page);
        await page
          .getByRole("button", { name: "Open service details", exact: true })
          .click();
        await browserExpect(page.locator("main h1")).toHaveText(
          "Child service",
        );
        await settle(page);
        await changeFixture(page, {
          hold: ["createKey"],
          fail: ["service:not_found"],
        });
        await page
          .getByRole("button", { name: "Create key", exact: true })
          .click();
        await page
          .getByRole("textbox", { name: "Key name", exact: true })
          .fill("Backend key");
        await page
          .getByRole("region", {
            name: "Service API keys for Child service",
            exact: true,
          })
          .getByRole("button", { name: /^Create New/ })
          .click();
        await page
          .getByRole("button", { name: "Refresh service", exact: true })
          .click();
        await browserExpect(
          page.getByText("The service is unavailable.", { exact: true }),
        ).toBeVisible();
        await finish(page, "createKey");
        await browserExpect(
          page.getByRole("button", { name: "Clear secret", exact: true }),
        ).toBeVisible();
        await page.evaluate(() => history.back());
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child",
        );
        expect(
          await page.evaluate(() => JSON.stringify(history.state)),
        ).not.toContain("synthetic-one-time");
        await browserExpect(
          page.getByText(keep[4][0], { exact: true }),
        ).toBeVisible();
        await context.grantPermissions(["clipboard-read", "clipboard-write"], {
          origin: "http://127.0.0.1:5174",
        });
        await page
          .getByRole("button", { name: "Copy secret", exact: true })
          .click();
        expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
          "synthetic-one-time-fixture-key",
        );
        await screenshot(page, "protected-secret");
        await page
          .getByRole("button", { name: "Clear secret", exact: true })
          .click();
        await browserExpect(
          page.getByText("synthetic-one-time-fixture-key", { exact: true }),
        ).toHaveCount(0);
        await page
          .getByRole("button", { name: "Back to services", exact: true })
          .click();
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/services");
      } finally {
        await close(context, errors);
      }
    }, 20000);
    it(`ignores exact-service results after navigation and keeps section errors independent at ${width}`, async () => {
      const { context, page, errors } = await open(width, height, {
        hold: ["service"],
      });
      try {
        await visit(page, "/services/child");
        await navigate(page, "Overview", width === 390);
        await settle(page);
        await finish(page, "service");
        await browserExpect(page.locator("main h1")).toHaveText("Overview");
        await changeFixture(page, { hold: [] });
        await navigate(page, "Services", width === 390);
        await settle(page);
        await page.locator('[data-service-api-name="child"]').dblclick();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child",
        );
        await settle(page);
        await patchValues(page, {
          workspaces: {
            items: [
              { ...metadata, api_name: "space", display_name: "Workspace one" },
            ],
            page: { has_more: false },
          },
        });
        await page
          .getByRole("button", { name: "Refresh workspaces", exact: true })
          .click();
        await settle(page);
        await changeFixture(page, { fail: ["workspaces"] });
        await page
          .getByRole("button", { name: "Refresh workspaces", exact: true })
          .click();
        await settle(page);
        await browserExpect(record(page, "Workspace one")).toBeVisible();
        await browserExpect(
          page.getByRole("button", { name: "Create workspace", exact: true }),
        ).toBeDisabled();
        await browserExpect(
          page.getByRole("button", { name: "Create key", exact: true }),
        ).toBeEnabled();
        await screenshot(page, "workspace-stale");
      } finally {
        await close(context, errors);
      }
    }, 20000);
    it(`keeps access loading, failures, long records and mutations local at ${width}`, async () => {
      const { context, page, errors } = await open(width, height, {
        hold: ["workspaces", "keys"],
      });
      try {
        await visit(page, "/services/%63hild");
        const workspaceRegion = page.getByRole("region", {
          name: "Workspaces for Child service",
          exact: true,
        });
        const keyRegion = page.getByRole("region", {
          name: "Service API keys for Child service",
          exact: true,
        });
        await browserExpect(
          workspaceRegion.getByText("Loading workspaces…", { exact: true }),
        ).toBeVisible();
        await browserExpect(
          keyRegion.getByText("Loading service API keys…", { exact: true }),
        ).toBeVisible();
        await screenshot(page, "access-loading");
        await finish(page, "workspaces", true);
        await finish(page, "keys", true);
        await screenshot(page, "access-unavailable");
        await changeFixture(page, { hold: [], fail: [] });
        await workspaceRegion
          .getByRole("button", { name: "Try again", exact: true })
          .click();
        await browserExpect(
          workspaceRegion.getByText("This service has no workspaces.", {
            exact: true,
          }),
        ).toBeVisible();
        await browserExpect(
          keyRegion.getByText("The service API keys are unavailable.", {
            exact: true,
          }),
        ).toBeVisible();
        await keyRegion
          .getByRole("button", { name: "Try again", exact: true })
          .click();
        await settle(page);
        await screenshot(page, "access-empty");
        const longWorkspace = "W".repeat(200);
        const longKey = "K".repeat(200);
        const workspace = {
          ...metadata,
          api_name: "w".repeat(63),
          display_name: longWorkspace,
        };
        const key = {
          ...metadata,
          id: "long-key",
          name: longKey,
          last_used_at: "2026-09-01T12:34:56Z",
        };
        await patchValues(page, {
          workspaces: { items: [workspace], page: { has_more: false } },
          keys: { items: [key], page: { has_more: false } },
          createWorkspace: {
            ...metadata,
            api_name: "created-space",
            display_name: "Created workspace",
          },
          deleteWorkspace: null,
          revokeKey: null,
        });
        await page
          .getByRole("button", { name: "Refresh workspaces", exact: true })
          .click();
        await page
          .getByRole("button", { name: "Refresh keys", exact: true })
          .click();
        await settle(page);
        await browserExpect(
          record(workspaceRegion, longWorkspace),
        ).toBeVisible();
        await browserExpect(record(keyRegion, longKey)).toBeVisible();
        await screenshot(page, "access-long-records");
        const textScale = await page.addStyleTag({
          content: "html { font-size: 200%; }",
        });
        await screenshot(page, "access-long-records-200");
        await textScale.evaluate((element) => element.remove());
        for (const region of [workspaceRegion, keyRegion]) {
          const viewport = region.locator(".od-data-table-desktop");
          if (width !== 390) {
            await browserExpect(viewport).toBeVisible();
            expect(
              await viewport.evaluate((element) => {
                element.scrollLeft = element.scrollWidth;
                return (
                  element.scrollWidth <= element.clientWidth ||
                  element.scrollLeft > 0
                );
              }),
            ).toBe(true);
          }
        }
        await changeFixture(page, { fail: ["keys"] });
        await page
          .getByRole("button", { name: "Refresh keys", exact: true })
          .click();
        await browserExpect(
          keyRegion.getByText(
            "The key records are stale. Refresh keys before you make changes.",
            { exact: true },
          ),
        ).toBeVisible();
        await browserExpect(
          page.getByRole("button", { name: "Create workspace", exact: true }),
        ).toBeEnabled();
        await browserExpect(
          page.getByRole("button", { name: "Create key", exact: true }),
        ).toBeDisabled();
        await screenshot(page, "keys-stale");
        await changeFixture(page, { fail: [] });
        await page
          .getByRole("button", { name: "Refresh keys", exact: true })
          .click();
        await settle(page);

        // Cancel both new-row forms without sending a write.
        await clearCalls(page);
        await page
          .getByRole("button", { name: "Create workspace", exact: true })
          .click();
        await workspaceRegion
          .getByRole("button", { name: /^Cancel New/ })
          .click();
        await page
          .getByRole("button", { name: "Create key", exact: true })
          .click();
        await keyRegion.getByRole("button", { name: /^Cancel New/ }).click();
        expect(await calls(page)).toEqual([]);
        await page
          .getByRole("button", { name: "Create workspace", exact: true })
          .click();
        await page
          .getByRole("textbox", { name: "Workspace display name", exact: true })
          .fill("Created workspace");
        await page
          .getByRole("textbox", { name: "Workspace API name", exact: true })
          .fill("created-space");
        await changeFixture(page, { hold: ["createWorkspace"] });
        await workspaceRegion
          .getByRole("button", { name: /^Create New/ })
          .click();
        await browserExpect(
          page.getByRole("button", { name: "Back to services", exact: true }),
        ).toBeDisabled();

        await finish(page, "createWorkspace", true);
        await browserExpect(
          page.getByRole("textbox", {
            name: "Workspace API name",
            exact: true,
          }),
        ).toHaveValue("created-space");
        await screenshot(page, "workspace-create-failed");
        await workspaceRegion
          .getByRole("button", { name: /^Create New/ })
          .click();
        await finish(page, "createWorkspace");
        await browserExpect(
          record(workspaceRegion, "Created workspace"),
        ).toBeVisible();
        await browserExpect(
          page.getByRole("textbox", {
            name: "Workspace API name",
            exact: true,
          }),
        ).toHaveCount(0);

        const deleteWorkspace = workspaceRegion.getByRole("button", {
          name: "Delete Created workspace",
          exact: true,
        });
        await deleteWorkspace.click();
        const dialog = page.getByRole("dialog");
        await browserExpect(
          dialog.getByRole("button", { name: "Cancel", exact: true }),
        ).toBeFocused();
        await screenshot(page, "workspace-delete-confirmation");
        await dialog
          .getByRole("button", { name: "Cancel", exact: true })
          .click();
        await browserExpect(deleteWorkspace).toBeFocused();
        await deleteWorkspace.click();
        await dialog
          .getByRole("textbox")
          .fill("Workspace created-space will be deleted.");
        await changeFixture(page, { hold: ["deleteWorkspace"] });
        await dialog
          .getByRole("button", { name: "Delete workspace", exact: true })
          .click();
        await finish(page, "deleteWorkspace", true);
        await browserExpect(
          dialog.getByText(
            "The row could not be deleted. The row is unchanged.",
            { exact: true },
          ),
        ).toBeVisible();
        // The retained cell also contains the inline deletion error.
        const retainedWorkspace = page
          .locator('section[aria-label="Workspaces for Child service"]')
          .locator(
            '[data-editable-table-row="created-space"][data-editable-table-cell="display-name"]',
          )
          .filter({ visible: true });
        await browserExpect(retainedWorkspace).toHaveCount(1);
        await browserExpect(retainedWorkspace).toHaveText(
          "Created workspaceThe row could not be deleted. The row is unchanged.",
        );
        await screenshot(page, "workspace-delete-failed");
        await dialog
          .getByRole("button", { name: "Delete workspace", exact: true })
          .click();
        await finish(page, "deleteWorkspace");
        await browserExpect(dialog).toHaveCount(0);
        await browserExpect(
          record(workspaceRegion, "Created workspace"),
        ).toHaveCount(0);

        await page
          .getByRole("button", { name: "Create key", exact: true })
          .click();
        await page
          .getByRole("textbox", { name: "Key name", exact: true })
          .fill("Backend key");
        await changeFixture(page, { hold: ["createKey"] });
        await keyRegion.getByRole("button", { name: /^Create New/ }).click();

        await finish(page, "createKey", true);
        await browserExpect(
          page.getByRole("textbox", { name: "Key name", exact: true }),
        ).toHaveValue("Backend key");
        await browserExpect(
          page.getByRole("region", { name: "One-time secret", exact: true }),
        ).toHaveCount(0);
        await browserExpect(record(keyRegion, "Fixture key")).toHaveCount(0);
        await screenshot(page, "key-create-failed");
        await keyRegion.getByRole("button", { name: /^Create New/ }).click();
        await finish(page, "createKey");
        await browserExpect(
          page.getByRole("region", { name: "One-time secret", exact: true }),
        ).toBeVisible();
        await screenshot(page, "key-secret");
        await page.evaluate(() => {
          Object.defineProperty(navigator.clipboard, "writeText", {
            configurable: true,
            value: () =>
              Promise.reject(new Error("Controlled clipboard failure")),
          });
        });
        await page
          .getByRole("button", { name: "Copy secret", exact: true })
          .click();
        await browserExpect(
          page.getByText(
            "The browser could not copy the secret. Select and copy it manually.",
            { exact: true },
          ),
        ).toBeVisible();
        await screenshot(page, "key-copy-failed");
        expect(
          await page.evaluate(() =>
            JSON.stringify({
              url: location.href,
              history: history.state,
              local: { ...localStorage },
              session: { ...sessionStorage },
              calls: window.shellFixture.calls,
            }),
          ),
        ).not.toContain("synthetic-one-time-fixture-key");
        await page
          .getByRole("button", { name: "Clear secret", exact: true })
          .click();
        await browserExpect(
          page.getByText("synthetic-one-time-fixture-key", { exact: true }),
        ).toHaveCount(0);
        await changeFixture(page, { hold: [] });
        await page
          .getByRole("button", { name: "Refresh keys", exact: true })
          .click();
        await settle(page);
        await browserExpect(
          page.getByRole("region", { name: "One-time secret", exact: true }),
        ).toHaveCount(0);
        const revoke = keyRegion.getByRole("button", {
          name: `Revoke ${longKey}`,
          exact: true,
        });
        await revoke.click();
        await screenshot(page, "key-revoke-confirmation");
        await dialog
          .getByRole("button", { name: "Cancel", exact: true })
          .click();
        await browserExpect(revoke).toBeFocused();
        await revoke.click();
        await dialog
          .getByRole("textbox")
          .fill(`Key ${longKey} will stop working.`);
        await changeFixture(page, { hold: ["revokeKey"] });
        await dialog
          .getByRole("button", { name: "Revoke key", exact: true })
          .click();
        await finish(page, "revokeKey", true);
        await browserExpect(record(keyRegion, longKey)).toHaveCount(1);
        await screenshot(page, "key-revoke-failed");
        await dialog
          .getByRole("button", { name: "Revoke key", exact: true })
          .click();
        await finish(page, "revokeKey");
        await browserExpect(record(keyRegion, longKey)).toHaveCount(0);
        const accessCalls = await page.evaluate(() =>
          window.shellFixture.calls.filter((call) =>
            [
              "workspaces",
              "keys",
              "createWorkspace",
              "deleteWorkspace",
              "createKey",
              "revokeKey",
            ].includes(call.name),
          ),
        );
        expect(accessCalls.length).toBeGreaterThan(8);
        for (const call of accessCalls) expect(call.args[0]).toBe("child");
        expect(
          accessCalls
            .filter((call) => call.name === "createWorkspace")
            .map((call) => call.args[1]),
        ).toEqual(
          Array(2).fill({
            api_name: "created-space",
            display_name: "Created workspace",
          }),
        );
        expect(
          accessCalls
            .filter((call) => call.name === "createKey")
            .map((call) => call.args[1]),
        ).toEqual(["Backend key", "Backend key"]);
      } finally {
        await close(context, errors);
      }
    }, 45000);

    it(`blocks dirty, pending and secret history, reload and close at ${width}`, async () => {
      const { context, page, errors } = await open(width, height);
      try {
        await visit(page, "/services?service=child");
        await settle(page);
        await page
          .getByRole("button", { name: "Open service details", exact: true })
          .click();
        await settle(page);
        await navigate(page, "Overview", width === 390);
        await settle(page);
        await page.goBack();
        await settle(page);
        const heading = page.locator("main h1");
        await browserExpect(heading).toHaveText("Child service");
        const displayName = page.getByRole("textbox", {
          name: "Display name",
          exact: true,
        });
        await displayName.fill("Unsaved child");
        for (const direction of ["back", "forward"]) {
          const question = page.waitForEvent("dialog");
          const restoration = blockedHistory(page, direction);
          const dialog = await question;
          expect(dialog.type()).toBe("confirm");
          await dialog.dismiss();
          await restoration;
          await browserExpect(page).toHaveURL(
            "http://127.0.0.1:5174/services/child",
          );
          await browserExpect(displayName).toHaveValue("Unsaved child");
        }
        await dismissUnload(page, "reload");
        await dismissUnload(page, "close");
        await changeFixture(page, { hold: ["updateService"] });
        await page
          .getByRole("button", { name: "Save changes", exact: true })
          .click();
        for (const direction of ["back", "forward"]) {
          await blockedHistory(page, direction);
          await browserExpect(page).toHaveURL(
            "http://127.0.0.1:5174/services/child",
          );
          await browserExpect(heading).toHaveText("Child service");
        }
        await dismissUnload(page, "reload");
        await dismissUnload(page, "close");
        await finish(page, "updateService");
        await settle(page);
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child",
        );
        // A failed or completed write must not replay an earlier navigation.
        await changeFixture(page, { hold: ["createKey"] });
        await page
          .getByRole("button", { name: "Create key", exact: true })
          .click();
        await page
          .getByRole("textbox", { name: "Key name", exact: true })
          .fill("Backend");
        await page
          .getByRole("region", {
            name: "Service API keys for Child service",
            exact: true,
          })
          .getByRole("button", { name: /^Create New/ })
          .click();
        await blockedHistory(page, "forward");
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child",
        );
        await dismissUnload(page, "reload");
        await finish(page, "createKey");
        await browserExpect(
          page.getByRole("button", { name: "Clear secret", exact: true }),
        ).toBeVisible();
        for (const direction of ["back", "forward"]) {
          await blockedHistory(page, direction);
          await browserExpect(page).toHaveURL(
            "http://127.0.0.1:5174/services/child",
          );
        }
        await navigate(page, "Overview", width === 390);
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child",
        );
        await dismissUnload(page, "reload");
        await dismissUnload(page, "close");
        await page
          .getByRole("button", { name: "Clear secret", exact: true })
          .click();
        await page.goForward();
        await browserExpect(heading).toHaveText("Overview");
        await browserExpect(heading).toBeFocused();
        await page.goBack();
        await settle(page);
        await browserExpect(
          page.getByRole("button", { name: "Clear secret", exact: true }),
        ).toHaveCount(0);
        await browserExpect(
          page.getByText("synthetic-one-time-fixture-key", { exact: true }),
        ).toHaveCount(0);
      } finally {
        await close(context, errors);
      }
    }, 30000);

    it(`rejects old access results after an exact service route change at ${width}`, async () => {
      const { context, page, errors } = await open(width, height, {
        hold: ["keys", "workspaces"],
      });
      try {
        await visit(page, "/services/child");
        await browserExpect(
          page.getByText("Loading workspaces…", { exact: true }),
        ).toBeVisible();
        await navigate(page, "Overview", width === 390);
        await settle(page);
        await changeFixture(page, { hold: [] });
        await patchValues(page, {
          "workspaces:root": {
            items: [
              {
                ...metadata,
                api_name: "root-space",
                display_name: "Root workspace",
              },
            ],
            page: { has_more: false },
          },
          "workspaces:child": {
            items: [
              {
                ...metadata,
                api_name: "child-space",
                display_name: "Child workspace",
              },
            ],
            page: { has_more: false },
          },
          "keys:root": {
            items: [{ ...metadata, id: "root-key", name: "Root key" }],
            page: { has_more: false },
          },
          "keys:child": {
            items: [{ ...metadata, id: "child-key", name: "Child key" }],
            page: { has_more: false },
          },
        });
        await navigate(page, "Services", width === 390);
        await settle(page);
        await page.locator('[data-service-api-name="root"]').press("Enter");
        await browserExpect(page.locator("main h1")).toHaveText("Root service");
        await settle(page);
        await finish(page, "workspaces");
        await finish(page, "keys");
        await browserExpect(record(page, "Root workspace")).toBeVisible();
        await browserExpect(record(page, "Root key")).toBeVisible();
        await browserExpect(
          page.getByText("Child workspace", { exact: true }),
        ).toHaveCount(0);
        await browserExpect(
          page.getByText("Child key", { exact: true }),
        ).toHaveCount(0);
        await browserExpect(page.locator("main h1")).toBeFocused();
      } finally {
        await close(context, errors);
      }
    }, 20000);

    it(`restores local graph scroll and supports pointer, keyboard and touch at ${width}`, async () => {
      const { context, page, errors } = await open(width, height);
      try {
        await visit(page, "/services");
        await settle(page);
        await patchValues(page, {
          services: {
            items: [
              rootService,
              ...Array.from({ length: 17 }, (_, index) => ({
                ...child,
                api_name: `child-${index}`,
                display_name: `Child ${index}`,
              })),
            ],
            page: { has_more: false },
          },
          "service:child-8": {
            ...child,
            api_name: "child-8",
            display_name: "Child 8",
          },
        });
        await page
          .getByRole("button", { name: "Refresh services", exact: true })
          .click();
        await settle(page);
        const node = page.locator('[data-service-api-name="child-8"]');
        if (width === 390) await node.tap();
        else await node.click();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services?service=child-8",
        );
        const inspector = page.locator(".od-graph-inspector");
        await browserExpect(inspector.locator("h2")).toBeFocused();
        await browserExpect(
          inspector.locator("footer").getByRole("button"),
        ).toHaveCount(1);
        await browserExpect(
          page.locator('[data-service-api-name][tabindex="0"]'),
        ).toHaveCount(1);
        const geometry = await page.evaluate(() => {
          const inspector = document.querySelector(".od-graph-inspector");
          const host = document.querySelector(".od-graph-workspace");
          const rect = inspector.getBoundingClientRect();
          const rem = parseFloat(
            getComputedStyle(document.documentElement).fontSize,
          );
          return {
            mode: inspector.dataset.mode,
            width: rect.width,
            left: rect.left,
            right: rect.right,
            bottom: rect.bottom,
            host: host.clientWidth,
            rem,
          };
        });
        const expectedMode =
          geometry.host >= 69 * geometry.rem
            ? "split"
            : geometry.host > 48 * geometry.rem
              ? "overlay"
              : "sheet";
        expect(geometry.mode).toBe(expectedMode);
        if (expectedMode === "sheet") {
          expect(geometry.left).toBeCloseTo(0.75 * geometry.rem, 0);
          expect(width - geometry.right).toBeCloseTo(0.75 * geometry.rem, 0);
          expect(height - geometry.bottom).toBeCloseTo(0.75 * geometry.rem, 0);
          const navigation = page.locator(".od-application-mobile-navigation");
          expect(
            await navigation.evaluate((element) => {
              const button = element.querySelector("button");
              const rect = button.getBoundingClientRect();
              return button.contains(
                document.elementFromPoint(
                  rect.x + rect.width / 2,
                  rect.y + rect.height / 2,
                ),
              );
            }),
          ).toBe(false);
          await navigation
            .locator("button")
            .evaluate((button) => button.focus());
          await browserExpect(inspector.locator("h2")).toBeFocused();
        } else expect(geometry.width).toBeCloseTo(21 * geometry.rem, 0);
        await screenshot(page, "graph-local-scroll");
        const viewport = page.getByRole("region", {
          name: "Services and parent relationships",
          exact: true,
        });
        const position = await viewport.evaluate((element) => ({
          left: element.scrollLeft,
          top: element.scrollTop,
          overflow: element.scrollWidth > element.clientWidth,
        }));
        expect(position.overflow).toBe(true);
        expect(position.left).toBeGreaterThan(0);
        await page
          .getByRole("button", { name: "Open service details", exact: true })
          .click();
        await settle(page);
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child-8",
        );
        await page.goBack();
        await settle(page);
        await browserExpect(inspector).toBeVisible();
        await browserExpect(
          expectedMode === "sheet" ? inspector.locator("h2") : node,
        ).toBeFocused();
        // The phone heading can receive focus before the restoration frame.
        await expect
          .poll(() =>
            viewport.evaluate((element) => ({
              left: element.scrollLeft,
              top: element.scrollTop,
            })),
          )
          .toEqual({ left: position.left, top: position.top });
        await inspector
          .getByRole("button", { name: "Close inspector", exact: true })
          .click();
        await browserExpect(inspector).toHaveCount(0);
        await browserExpect(node).toBeFocused();
        await node.press("Space");
        await browserExpect(inspector.locator("h2")).toBeFocused();
        await page.keyboard.press("Escape");
        await browserExpect(inspector).toHaveCount(0);
        await browserExpect(node).toBeFocused();
        await node.press("Enter");
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services/child-8",
        );
      } finally {
        await close(context, errors);
      }
    }, 20000);

    for (const section of ["workspace", "key"]) {
      it(`checks pending ${section} creation with Axe at ${width}`, async () => {
        const { context, page, errors } = await open(width, height);
        try {
          await visit(page, "/services/child");
          await settle(page);
          await patchValues(page, {
            createWorkspace: {
              ...metadata,
              api_name: "created-space",
              display_name: "Created workspace",
            },
          });
          const operation =
            section === "workspace" ? "createWorkspace" : "createKey";
          await changeFixture(page, { hold: [operation] });
          await page
            .getByRole("button", {
              name: section === "workspace" ? "Create workspace" : "Create key",
              exact: true,
            })
            .click();
          if (section === "workspace") {
            await page
              .getByRole("textbox", {
                name: "Workspace display name",
                exact: true,
              })
              .fill("Created workspace");
            await page
              .getByRole("textbox", { name: "Workspace API name", exact: true })
              .fill("created-space");
          } else
            await page
              .getByRole("textbox", { name: "Key name", exact: true })
              .fill("Backend key");
          await page.getByRole("button", { name: /^Create New/ }).click();
          await browserExpect(
            page.getByRole("button", { name: "Back to services", exact: true }),
          ).toBeDisabled();
          await screenshot(page, `${section}-create-pending`);
        } finally {
          await close(context, errors);
        }
      }, 10000);
    }
  }
});
