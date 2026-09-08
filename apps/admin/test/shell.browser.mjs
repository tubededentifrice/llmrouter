// Run: node apps/admin/test/shell.browser.mjs
/* global window, document, innerWidth, sessionStorage, getComputedStyle */
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
  process.env.LLMROUTER_SHELL_EVIDENCE ?? "/tmp/llmrouter-shell-browser",
);
const labels = [
  "Overview",
  "Services",
  "LLM configuration",
  "Logs",
  "Usage & cost",
  "Activity & health",
];
const routes = [
  "overview",
  "services",
  "configuration",
  "logs",
  "statistics",
  "operations",
];
const overviewMethods = ["services", "providers", "providerModels", "health"];
const catalogMethods = [
  "services",
  "providers",
  "models",
  "providerModels",
  "credentials",
  "assignments",
];
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
async function screenshot(page, name) {
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: resolve(evidence, `${name}.png`),
    fullPage: true,
  });
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
async function finishAll(page, fail = false) {
  await page.evaluate((failure) => {
    const fixture = window.shellFixture;
    fixture.hold = [];
    while (fixture.pending.length)
      fixture.finish(fixture.pending[0]?.name ?? "", failure);
  }, fail);
}
async function close(context, errors) {
  await context.close();
  expect(errors).toEqual([]);
}
async function editAssignment(page) {
  await page
    .getByRole("button", { name: "Add assignment", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Assignment API name", exact: true })
    .fill("draft");
  await page
    .getByRole("textbox", { name: "Display name", exact: true })
    .fill("Draft assignment");
}
async function discardAssignment(page) {
  await page
    .getByRole("button", { name: "Discard changes", exact: true })
    .click();
  await confirmImpact(
    page.getByRole("dialog", {
      name: "Discard assignment changes?",
      exact: true,
    }),
    "Discard changes",
  );
}
async function confirmImpact(dialog, label) {
  await dialog
    .getByRole("textbox", {
      name: "Enter the impact statement to continue",
      exact: true,
    })
    .fill(
      await dialog
        .locator(".od-confirmation-dialog-impact strong")
        .textContent(),
    );
  await dialog.getByRole("button", { name: label, exact: true }).click();
}
async function startKey(page) {
  await page.getByRole("button", { name: "Create key", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Key name", exact: true })
    .fill("Fixture key");
  await page.getByRole("button", { name: /^Create New/ }).click();
  await browserExpect.poll(() => calls(page)).toContain("createKey");
}
async function finish(page, name, fail = false, service) {
  await page.evaluate(
    ({ name, fail, service }) =>
      window.shellFixture.finish(name, fail, service),
    { name, fail, service },
  );
}
async function assignmentValues(page) {
  await page.evaluate(() => {
    for (const service of ["root", "child"])
      window.shellFixture.values[`assignments:${service}`] = {
        items: [
          {
            api_name: `${service}-assignment`,
            display_name: `${service} assignment`,
            definition_kind: "direct_chain",
            defined_by_service_api_name: service,
            direct_chain: [{ provider_model_api_name: "route" }],
            effective_chain: [{ provider_model_api_name: "route" }],
            observed_requirements: [],
          },
        ],
        page: { has_more: false },
      };
  });
}
describe("authenticated shell in a real browser", () => {
  it("expires the authenticated page during a protected service write", async () => {
    const { context, page, errors } = await open(1440, 1000, {
      hold: ["createKey"],
    });
    try {
      await page.clock.install();
      await visit(page, "/services?service=root");
      await settle(page);
      await startKey(page);
      await page.clock.fastForward(3_600_001);
      await browserExpect(
        page.getByRole("heading", {
          name: "Your session expired",
          exact: true,
        }),
      ).toBeVisible();
      await browserExpect(page.locator(".od-application-shell")).toHaveCount(0);
      await finish(page, "createKey");
      await browserExpect(
        page.getByRole("heading", {
          name: "Your session expired",
          exact: true,
        }),
      ).toBeVisible();
      await browserExpect(
        page.getByRole("button", { name: "Clear key", exact: true }),
      ).toHaveCount(0);
    } finally {
      await close(context, errors);
    }
  }, 20_000);
  it("keeps pending assignment writes in their selected service", async () => {
    const { context, page, errors } = await open(1440, 1000, {
      hold: ["putAssignment"],
    });
    try {
      await visit(page, "/overview");
      await navigate(page, "LLM configuration", false);
      await settle(page);
      const service = page.getByRole("combobox", {
        name: "Service context",
        exact: true,
      });
      await service.selectOption("root");
      await settle(page);
      await editAssignment(page);
      await page
        .getByRole("combobox", { name: "Definition", exact: true })
        .selectOption("inherit");
      await page
        .getByRole("textbox", { name: "Inherited assignment", exact: true })
        .fill("source");
      await page
        .getByRole("button", {
          name: "Save selected service assignment",
          exact: true,
        })
        .click();
      await browserExpect.poll(() => calls(page)).toContain("putAssignment");
      await browserExpect(service).toBeDisabled();
      await page
        .locator(".od-application-sidebar")
        .getByRole("link", { name: "Overview", exact: true })
        .click();
      await browserExpect(page).toHaveURL(
        "http://127.0.0.1:5174/configuration?service=root",
      );
      await page.goBack();
      await browserExpect(page).toHaveURL(
        "http://127.0.0.1:5174/configuration?service=root",
      );
      await finish(page, "putAssignment", true);
      await browserExpect(service).toBeEnabled();
      await browserExpect(
        page.getByRole("textbox", { name: "Assignment API name", exact: true }),
      ).toHaveValue("draft");
      await screenshot(page, "configuration-failed-write");
      await discardAssignment(page);
      await page.goBack();
      await browserExpect(page).toHaveURL("http://127.0.0.1:5174/overview");
    } finally {
      await close(context, errors);
    }
  }, 30_000);
  it("reserves a non-zero phone safe area and keeps the last content reachable", async () => {
    const { context, page, errors } = await open(390, 844);
    try {
      const cdp = await context.newCDPSession(page);
      await cdp.send("Emulation.setSafeAreaInsetsOverride", {
        insets: { bottom: 24 },
      });
      await visit(page, "/operations");
      await settle(page);
      await page.evaluate(() =>
        window.scrollTo(0, document.documentElement.scrollHeight),
      );
      const bounds = await page.evaluate(() => {
        const navigation = document.querySelector(
          ".od-application-mobile-navigation",
        );
        const row = navigation.querySelector(".od-mobile-navigation");
        const main = document.querySelector("main");
        const last = main.querySelector(
          ".administration-page",
        ).lastElementChild;
        return {
          padding: Number.parseFloat(getComputedStyle(row).paddingBottom),
          navTop: navigation.getBoundingClientRect().top,
          contentBottom: last.getBoundingClientRect().bottom,
        };
      });
      expect(bounds.padding).toBe(24);
      expect(bounds.contentBottom).toBeLessThanOrEqual(bounds.navTop + 1);
      await screenshot(page, "phone-safe-area-24");
      await cdp.detach();
    } finally {
      await close(context, errors);
    }
  }, 20_000);
  it("fences assignments across service changes and failed B loads", async () => {
    const { context, page, errors } = await open(1440, 1000);
    try {
      for (const lateFailure of [false, true]) {
        await visit(page, "/configuration");
        await settle(page);
        await assignmentValues(page);
        await changeFixture(page, { hold: ["assignments"] });
        const service = page.getByRole("combobox", {
          name: "Service context",
          exact: true,
        });
        await service.selectOption("root");
        await browserExpect
          .poll(() => page.evaluate(() => window.shellFixture.pending.length))
          .toBe(1);
        await service.selectOption("child");
        await browserExpect
          .poll(() => page.evaluate(() => window.shellFixture.pending.length))
          .toBe(2);
        await finish(page, "assignments", false, "child");
        await browserExpect(
          page.locator('[data-node-id="assignment:child-assignment"]'),
        ).toBeVisible();
        await finish(page, "assignments", lateFailure, "root");
        await browserExpect(
          page.locator('[data-node-id="assignment:child-assignment"]'),
        ).toBeVisible();
        await browserExpect(
          page.locator('[data-node-id="assignment:root-assignment"]'),
        ).toHaveCount(0);
        await browserExpect(
          page.getByText("Assignments are stale or unavailable.", {
            exact: true,
          }),
        ).toHaveCount(0);
      }
      await changeFixture(page, { hold: [] });
      const service = page.getByRole("combobox", {
        name: "Service context",
        exact: true,
      });
      await service.selectOption("root");
      await browserExpect(
        page.locator('[data-node-id="assignment:root-assignment"]'),
      ).toBeVisible();
      await changeFixture(page, { fail: ["assignments"] });
      await service.selectOption("child");
      await browserExpect(
        page.getByText("Assignments are stale or unavailable.", {
          exact: true,
        }),
      ).toBeVisible();
      await browserExpect(
        page.locator('[data-node-id="assignment:root-assignment"]'),
      ).toHaveCount(0);
      await browserExpect(service).toHaveValue("child");
    } finally {
      await close(context, errors);
    }
  }, 30_000);
  for (const phone of [false, true]) {
    it(`keeps dirty configuration values and selector focus (${phone ? "phone" : "desktop"})`, async () => {
      const { context, page, errors } = await open(
        phone ? 390 : 1440,
        phone ? 844 : 1000,
      );
      try {
        await visit(page, "/configuration?service=root");
        await settle(page);
        await editAssignment(page);
        // At phone width the assignment inspector is modal. Close requests open its discard confirmation.
        if (phone) {
          await page.keyboard.press("Escape");
          await page
            .getByRole("dialog", {
              name: "Discard assignment changes?",
              exact: true,
            })
            .getByRole("button", { name: "Cancel", exact: true })
            .click();
          await browserExpect(
            page.getByRole("textbox", {
              name: "Assignment API name",
              exact: true,
            }),
          ).toHaveValue("draft");
          await discardAssignment(page);
          return;
        }
        const service = page.getByRole("combobox", {
          name: "Service context",
          exact: true,
        });
        await service.focus();
        await service.selectOption("child");
        const dialog = page.getByRole("dialog", {
          name: "Discard assignment changes?",
          exact: true,
        });
        await browserExpect(dialog).toBeVisible();
        expect((await new AxeBuilder({ page }).analyze()).violations).toEqual(
          [],
        );
        await screenshot(page, "desktop-discard-service");
        await dialog
          .getByRole("button", { name: "Cancel", exact: true })
          .click();
        await browserExpect(service).toBeFocused();
        await browserExpect(service).toHaveValue("root");
        await browserExpect(
          page.getByRole("textbox", {
            name: "Assignment API name",
            exact: true,
          }),
        ).toHaveValue("draft");
        await page
          .locator(".od-application-sidebar")
          .getByRole("link", { name: "Overview", exact: true })
          .click();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/configuration?service=root",
        );
        await service.selectOption("child");
        await confirmImpact(dialog, "Discard and change service");
        await browserExpect(service).toBeFocused();
        await browserExpect(service).toHaveValue("child");
      } finally {
        await close(context, errors);
      }
    }, 30_000);
  }
  for (const phone of [false, true]) {
    it(`audit: rejected history preserves entries (${phone ? "phone" : "desktop"})`, async () => {
      const { context, page, errors } = await open(
        phone ? 390 : 1440,
        phone ? 844 : 1000,
      );
      try {
        await visit(page, "/overview");
        await navigate(page, "LLM configuration", phone);
        await settle(page);
        await page
          .getByRole("combobox", { name: "Service context", exact: true })
          .selectOption("root");
        await settle(page);
        await editAssignment(page);
        await page.goBack();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/configuration?service=root",
        );
        await browserExpect(
          page.getByRole("textbox", {
            name: "Assignment API name",
            exact: true,
          }),
        ).toHaveValue("draft");
        await discardAssignment(page);
        await page.goBack();
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/overview");
        await page.goForward();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/configuration?service=root",
        );
        await navigate(page, "Logs", phone);
        await page.goBack();
        await settle(page);
        await editAssignment(page);
        await page.goForward();
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/configuration?service=root",
        );
        await browserExpect(
          page.getByRole("textbox", {
            name: "Assignment API name",
            exact: true,
          }),
        ).toHaveValue("draft");
        await discardAssignment(page);
        await page.goForward();
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/logs");
      } finally {
        await close(context, errors);
      }
    }, 25_000);
  }
  it("audit: Services retains a pending one-time key on shell navigation", async () => {
    const { context, page, errors } = await open(1440, 1000, {
      hold: ["createKey"],
    });
    try {
      await visit(page, "/services?service=root");
      await settle(page);
      await startKey(page);
      await page
        .locator(".od-application-sidebar")
        .getByRole("link", { name: "Overview", exact: true })
        .click();
      await browserExpect(page).toHaveURL(
        "http://127.0.0.1:5174/services?service=root",
      );
      await finish(page, "createKey");
      await browserExpect(
        page.getByRole("button", { name: "Clear key", exact: true }),
      ).toBeVisible();
      await page
        .locator(".od-application-sidebar")
        .getByRole("link", { name: "Overview", exact: true })
        .click();
      await browserExpect(page).toHaveURL(
        "http://127.0.0.1:5174/services?service=root",
      );
      await page
        .locator(".od-application-sidebar")
        .getByRole("button", { name: "Sign out", exact: true })
        .click();
      expect(await calls(page)).not.toContain("logout");
      await screenshot(page, "services-protected-key");
      await page
        .getByRole("button", { name: "Clear key", exact: true })
        .click();
      await navigate(page, "Overview", false);
    } finally {
      await close(context, errors);
    }
  }, 20_000);
  it("audit: preserves initial Services and configuration refresh focus", async () => {
    const { context, page, errors } = await open(1440, 1000, {
      hold: catalogMethods,
    });
    try {
      for (const [path, label] of [
        ["/services", "Refresh services"],
        ["/configuration", "Refresh configuration"],
      ]) {
        await visit(page, path);
        const refresh = page.getByRole("button", { name: label, exact: true });
        await refresh.focus();
        await browserExpect(refresh).toHaveAttribute("aria-busy", "true");
        await finishAll(page);
        await settle(page);
        await browserExpect(refresh).toBeFocused();
      }
    } finally {
      await close(context, errors);
    }
  }, 20_000);
  it("audit: Operations waits for initial responses", async () => {
    const { context, page, errors } = await open(1440, 1000, {
      hold: ["health", "retention", "providerModels", "activityPage"],
    });
    try {
      await visit(page, "/operations");
      await browserExpect(
        page.getByText("Health unavailable", { exact: true }),
      ).toHaveCount(0);
      await browserExpect(
        page.getByText("Retention unavailable", { exact: true }),
      ).toHaveCount(0);
      await browserExpect(
        page.getByText("No current cooldowns", { exact: true }),
      ).toHaveCount(0);
      await screenshot(page, "operations-initial-loading");
      const refresh = page.getByRole("button", {
        name: "Refresh operations",
        exact: true,
      });
      await refresh.focus();
      for (const name of ["retention", "providerModels", "health"])
        await finish(page, name);
      await browserExpect(refresh).toHaveAttribute("aria-busy", "true");
      await browserExpect(
        page.getByText("Loading retained activity", { exact: true }),
      ).toBeVisible();
      await clearCalls(page);
      await refresh.press("Enter");
      expect(await calls(page)).toEqual([]);
      await finish(page, "activityPage");
      await browserExpect(refresh).toHaveAttribute("aria-busy", "false");
      await browserExpect(refresh).toBeFocused();
    } finally {
      await close(context, errors);
    }
  }, 15_000);
  for (const { width, height } of [
    { width: 1440, height: 1000 },
    { width: 1100, height: 800 },
    { width: 390, height: 844 },
  ]) {
    const phone = width === 390;
    it(`keeps local loading, failure, retry, and stale states at ${String(width)}`, async () => {
      const { context, page, errors } = await open(width, height, {
        hold: [...catalogMethods, "health", "retention", "activityPage"],
      });
      try {
        for (const [path, action, retry] of [
          ["services", "Refresh services", "Retry services"],
          ["configuration", "Refresh configuration", "Retry configuration"],
          ["operations", "Refresh operations", "Retry operations"],
        ]) {
          await visit(page, `/${path}`);
          await shellGeometry(page, phone, height);
          await browserExpect(
            page.getByRole("button", { name: action, exact: true }),
          ).toHaveAttribute("aria-busy", "true");
          if (path === "services")
            await browserExpect(
              page.getByRole("button", { name: "Create service", exact: true }),
            ).toHaveCount(0);
          await screenshot(page, `${width}-${path}-loading`);
          await finishAll(page, true);
          await browserExpect(
            page.getByRole("button", { name: retry, exact: true }).first(),
          ).toBeVisible();
          await screenshot(page, `${width}-${path}-failure`);
          await page
            .getByRole("button", { name: retry, exact: true })
            .first()
            .click();
          if (path === "operations")
            await page
              .getByRole("button", {
                name: "Try loading activity again",
                exact: true,
              })
              .click();
          await settle(page);
          await changeFixture(page, {
            fail:
              path === "services"
                ? ["services"]
                : path === "configuration"
                  ? ["models"]
                  : ["health"],
          });
          await page.getByRole("button", { name: action, exact: true }).click();
          await settle(page);
          await browserExpect(
            page.getByText(/is stale\./).first(),
          ).toBeVisible();
          await screenshot(page, `${width}-${path}-stale`);
        }
      } finally {
        await close(context, errors);
      }
    }, 60_000);
    it(`keeps route ownership, navigation, history, and geometry at ${String(width)}`, async () => {
      const { context, page, errors } = await open(width, height);
      try {
        await visit(page, "/logs");
        await visit(page, "/");
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/overview");
        await settle(page);
        await browserExpect(page.locator("main h1")).toHaveText("Overview");
        await browserExpect(page).toHaveTitle(/^Overview/);
        for (const [label, count] of [
          ["Services", "2"],
          ["Provider connections", "1"],
          ["Provider-models", "1"],
          ["Current cooldowns", "1"],
        ]) {
          const card = page
            .locator(".od-stat-card")
            .filter({ has: page.getByText(label ?? "", { exact: true }) });
          await browserExpect(card.locator("strong")).toHaveText(count ?? "");
        }
        await browserExpect(
          page.getByText("healthy", { exact: true }).first(),
        ).toBeVisible();
        await shellGeometry(page, phone, height);
        await screenshot(page, `${String(width)}-overview`);
        const skip = page.getByRole("link", {
          name: "Skip to content",
          exact: true,
        });
        await page.locator("main h1").focus();
        await page.keyboard.press("Tab");
        await browserExpect(
          page.getByRole("button", { name: "Refresh overview", exact: true }),
        ).toBeFocused();
        // Shift+Tab reaches the first document tab stop without programmatic focus on the link.
        for (let attempt = 0; attempt < 14; attempt++) {
          if (
            await skip.evaluate((element) => element === document.activeElement)
          )
            break;
          await page.keyboard.press("Shift+Tab");
        }
        await browserExpect(skip).toBeFocused();
        if (phone) {
          await page.keyboard.press("Tab");
          await browserExpect(
            page.getByRole("button", { name: "Refresh overview", exact: true }),
          ).toBeFocused();
          await page.keyboard.press("Tab");
          await browserExpect(
            page
              .locator(".od-application-mobile-navigation")
              .getByRole("link", { name: "Overview", exact: true }),
          ).toBeFocused();
          await page.keyboard.press("Tab");
          await browserExpect(
            page.getByRole("button", { name: "Navigation", exact: true }),
          ).toBeFocused();
        } else {
          for (const label of labels) {
            await page.keyboard.press("Tab");
            await browserExpect(
              page
                .locator(".od-application-sidebar")
                .getByRole("link", { name: label, exact: true }),
            ).toBeFocused();
          }
          await page.keyboard.press("Tab");
          await browserExpect(
            page
              .locator(".od-application-sidebar")
              .getByRole("button", { name: /Fixture administrator/ }),
          ).toBeFocused();
          await page.keyboard.press("Tab");
          await browserExpect(
            page
              .locator(".od-application-sidebar")
              .getByRole("button", { name: "Sign out", exact: true }),
          ).toBeFocused();
        }
        await skip.focus();
        await skip.press("Enter");
        await browserExpect(page.locator("main h1")).toBeFocused();
        const axe = await new AxeBuilder({ page }).analyze();
        expect(axe.violations).toEqual([]);
        await page.goBack();
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/logs");
        await page.goForward();
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/overview");
        if (phone) {
          const trigger = page.getByRole("button", {
            name: "Navigation",
            exact: true,
          });
          await trigger.click();
          const dialog = page.getByRole("dialog");
          expect(await dialog.getByRole("link").allTextContents()).toEqual(
            labels,
          );
          await browserExpect(
            dialog.getByText("LLM Router", { exact: true }),
          ).toBeVisible();
          await browserExpect(
            dialog.getByRole("region", { name: "Administrator", exact: true }),
          ).toContainText("Fixture administrator");
          for (const label of labels) {
            await browserExpect(
              dialog.getByRole("link", { name: label, exact: true }),
            ).toBeFocused();
            await page.keyboard.press("Tab");
          }
          await browserExpect(
            dialog.getByRole("button", { name: "Sign out", exact: true }),
          ).toBeFocused();
          expect((await new AxeBuilder({ page }).analyze()).violations).toEqual(
            [],
          );
          await screenshot(page, `${String(width)}-navigation`);
          await page.keyboard.press("Escape");
          await browserExpect(trigger).toBeFocused();
        } else
          expect(
            await page
              .locator(".od-application-sidebar")
              .getByRole("link")
              .allTextContents(),
          ).toEqual(labels);
        for (let index = 0; index < routes.length; index++) {
          await navigate(page, labels[index] ?? "", phone);
          await settle(page);
          await browserExpect(page).toHaveURL(
            `http://127.0.0.1:5174/${routes[index] ?? ""}`,
          );
          await shellGeometry(page, phone, height);
          expect((await new AxeBuilder({ page }).analyze()).violations).toEqual(
            [],
          );
          if (!phone)
            await browserExpect(
              page.locator('.od-application-sidebar [aria-current="page"]'),
            ).toHaveText(labels[index] ?? "");
          await screenshot(
            page,
            `${String(width)}-${routes[index] ?? "route"}`,
          );
        }
        await visit(page, "/configuration?service=root");
        await settle(page);
        const service = page.getByRole("combobox", {
          name: "Service context",
          exact: true,
        });
        await browserExpect(service).toHaveValue("root");
        await navigate(page, "Services", phone);
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/services?service=root",
        );
        if (phone) {
          // The selected service uses a modal inspector. Close it before navigation.
          await browserExpect(page.getByRole("dialog")).toBeVisible();
          await page.keyboard.press("Escape");
          await browserExpect(page.getByRole("dialog")).toHaveCount(0);
          await browserExpect(page).toHaveURL("http://127.0.0.1:5174/services");
        }
        await navigate(page, "LLM configuration", phone);
        await browserExpect(service).toHaveValue(phone ? "" : "root");
        if (phone) await service.selectOption("root");
        await navigate(page, "Logs", phone);
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/logs");
        await page.goBack();
        await browserExpect(service).toHaveValue("root");
        await page.goForward();
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/logs");
        // Service-details route content belongs to the later details task.
        await navigate(page, "LLM configuration", phone);
        await browserExpect(service).toHaveValue("");
        await service.selectOption("root");
        await service.selectOption("");
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/configuration",
        );
        await visit(page, "/configuration?service=missing");
        await settle(page);
        await browserExpect(service).toHaveValue("");
        await browserExpect(page).toHaveURL(
          "http://127.0.0.1:5174/configuration",
        );
        for (const route of ["overview", "logs", "statistics", "operations"]) {
          await visit(page, `/${route}?service=root`);
          await browserExpect(page).toHaveURL(`http://127.0.0.1:5174/${route}`);
        }
        await visit(page, "/configuration?service=root");
        await settle(page);
        await page.addStyleTag({ content: "html { font-size: 200%; }" });
        const readableValue = await service.evaluate((select) => {
          const style = getComputedStyle(select);
          const canvas = document.createElement("canvas");
          const context = canvas.getContext("2d");
          context.font = style.font;
          const firstWord = select.selectedOptions[0].text.split(/\s+/)[0];
          return {
            // Reserve one font-size for the native select arrow.
            available:
              select.clientWidth -
              Number.parseFloat(style.paddingLeft) -
              Number.parseFloat(style.paddingRight) -
              Number.parseFloat(style.fontSize),
            wordWidth: context.measureText(firstWord).width,
          };
        });
        expect(readableValue.available).toBeGreaterThanOrEqual(
          readableValue.wordWidth,
        );
        await service.focus();
        await browserExpect(service).toBeFocused();
        await page.keyboard.press("Tab");
        await browserExpect(
          page.getByRole("searchbox", {
            name: "Search configuration",
            exact: true,
          }),
        ).toBeFocused();
        await page.keyboard.press("Tab");
        await browserExpect(
          page.getByRole("button", {
            name: "Refresh configuration",
            exact: true,
          }),
        ).toBeFocused();
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await screenshot(page, `${String(width)}-configuration-text-200`);
      } finally {
        await close(context, errors);
      }
    }, 90_000);
    it(`keeps Overview states local and rejects stale responses at ${String(width)}`, async () => {
      const { context, page, errors } = await open(width, height, {
        hold: overviewMethods,
      });
      try {
        await visit(page, "/overview");
        await browserExpect(
          page.getByText("Loading Overview.", { exact: true }),
        ).toBeVisible();
        await shellGeometry(page, phone, height);
        await screenshot(page, `${String(width)}-overview-loading`);
        await finishAll(page, true);
        await browserExpect(
          page.getByText("Overview is unavailable.", { exact: true }),
        ).toBeVisible();
        await screenshot(page, `${String(width)}-overview-unavailable`);
        await page
          .getByRole("button", { name: "Retry Overview", exact: true })
          .first()
          .click();
        await settle(page);
        await clearCalls(page);
        await changeFixture(page, { hold: overviewMethods });
        const refresh = page.getByRole("button", {
          name: "Refresh overview",
          exact: true,
        });
        await refresh.click();
        await browserExpect(refresh).toBeFocused();
        await browserExpect(refresh).toHaveAttribute("aria-busy", "true");
        await refresh.press("Enter");
        expect((await calls(page)).sort()).toEqual([...overviewMethods].sort());
        await finishAll(page, true);
        await browserExpect(refresh).toBeFocused();
        await browserExpect(page.getByText(/stale/i).first()).toBeVisible();
        await screenshot(page, `${String(width)}-overview-stale`);
        await changeFixture(page, { fail: ["health"] });
        await refresh.click();
        await settle(page);
        await browserExpect(
          page.getByText("Provider connections", { exact: true }),
        ).toBeVisible();
        await screenshot(page, `${String(width)}-overview-partial`);
        await changeFixture(page, { fail: [], hold: overviewMethods });
        await refresh.click();
        await navigate(page, "Logs", phone);
        const heading = page.locator("main h1");
        const before = await heading.textContent();
        await finishAll(page);
        await browserExpect(heading).toHaveText(before ?? "");
        await browserExpect(heading).toBeFocused();
        await browserExpect(page).toHaveURL("http://127.0.0.1:5174/logs");
      } finally {
        await close(context, errors);
      }
    }, 60_000);
  }
  it("uses exact local refresh reads and preserves configuration context", async () => {
    const { context, page, errors } = await open(1440, 1000);
    try {
      for (const [path, action, methods] of [
        ["/services", "Refresh services", ["services"]],
        [
          "/configuration?service=root",
          "Refresh configuration",
          catalogMethods,
        ],
        [
          "/operations",
          "Refresh operations",
          ["health", "retention", "providerModels", "activityPage"],
        ],
      ]) {
        await visit(page, path);
        await settle(page);
        await clearCalls(page);
        await changeFixture(page, { hold: [...methods] });
        const refresh = page.getByRole("button", { name: action, exact: true });
        await refresh.click();
        await browserExpect(refresh).toHaveAttribute("aria-busy", "true");
        await browserExpect(refresh).toBeFocused();
        await refresh.press("Enter");
        expect((await calls(page)).sort()).toEqual([...methods].sort());
        await finishAll(page);
        await browserExpect(refresh).toBeFocused();
        await settle(page);
        if (path.startsWith("/configuration"))
          await browserExpect(
            page.getByRole("combobox", {
              name: "Service context",
              exact: true,
            }),
          ).toHaveValue("root");
      }
    } finally {
      await close(context, errors);
    }
  }, 60_000);
  for (const { width, height } of [
    { width: 1440, height: 1000 },
    { width: 1100, height: 800 },
    { width: 390, height: 844 },
  ]) {
    it(`retains confirmed activity on failed refresh at ${String(width)}`, async () => {
      const { context, page, errors } = await open(width, height);
      try {
        await visit(page, "/operations");
        await settle(page);
        await page.evaluate(() => {
          window.shellFixture.values.activityPage = {
            items: [
              {
                id: "review-activity",
                actor_subject: "fixture-administrator",
                action: "review_action",
                resource_type: "service",
                resource_api_name: "root",
                result: "succeeded",
                occurred_at: "2026-08-25T00:00:00Z",
              },
            ],
            page: { has_more: false },
          };
        });
        const operations = page.getByRole("button", {
          name: "Refresh operations",
          exact: true,
        });
        await operations.click();
        await settle(page);
        await changeFixture(page, { fail: ["activityPage"] });
        await operations.click();
        await settle(page);
        await browserExpect(
          page
            .getByText("review_action", { exact: true })
            .filter({ visible: true }),
        ).toBeVisible();
        await browserExpect(
          page.getByText(
            "Retained activity is stale. Refresh activity to try again.",
            { exact: true },
          ),
        ).toBeVisible();
        await browserExpect(operations).toBeFocused();
        await screenshot(page, `${width}-activity-stale-records`);
      } finally {
        await close(context, errors);
      }
    }, 30_000);
  }
  it("lands at Overview after default sign-in", async () => {
    const { context, page, errors } = await open(1440, 1000, {
      signedOut: true,
    });
    try {
      await page.goto("http://127.0.0.1:5174/");
      await page
        .getByRole("button", { name: "Continue with Pocket ID", exact: true })
        .click();
      await browserExpect(page).toHaveURL("http://127.0.0.1:5174/overview");
      await browserExpect(page.locator("main h1")).toHaveText("Overview");
      expect(
        await page.evaluate(() =>
          sessionStorage.getItem("shell-sign-in-target"),
        ),
      ).toBe("/overview");
    } finally {
      await close(context, errors);
    }
  }, 20_000);
});
