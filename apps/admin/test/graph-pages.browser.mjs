// Run: node apps/admin/test/graph-pages.browser.mjs
/* global window, document, innerWidth, innerHeight, getComputedStyle, requestAnimationFrame */
// Controlled real-App localhost reads. No live write, session, or provider call.
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { URL } from "node:url";
import { after, before, describe, it } from "node:test";
const root = resolve(import.meta.dirname, "../../..");
const requireTools = createRequire(resolve(root, "../opendle-ui/package.json"));
const { chromium, expect } = requireTools("@playwright/test");
const { build } = requireTools("esbuild");
const { default: AxeBuilder } = requireTools("@axe-core/playwright");
const shared = dirname(
  createRequire(import.meta.url).resolve("@opendle/ui/package.json"),
);
const evidence = resolve(
  process.env.LLMROUTER_GRAPH_EVIDENCE ?? "/tmp/llmr-spq8-graph-browser",
);
const measured = [];
const methods = [
  "services",
  "providers",
  "models",
  "providerModels",
  "credentials",
];
const sizes = [
  [1440, 1000],
  [1100, 800],
  [390, 844],
];
const names = {
  services: {
    heading: "Services",
    viewport: "Services and parent relationships",
    first: "Refresh services",
    host: ".od-graph-workspace",
    viewportSelector: ".od-graph-viewport",
  },
  configuration: {
    heading: "LLM configuration",
    viewport: "LLM configuration relationships",
    first: "Service context",
    host: ".od-relationship-graph",
    viewportSelector: ".od-relationship-graph-viewport",
  },
};
let browser;
let script;
let css;
before(async () => {
  const result = await build({
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
async function open(width, height, route, boot = {}) {
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", async (request) => {
    const url = new URL(request.request().url());
    expect(url.origin).toBe("http://127.0.0.1:5174");
    if (url.pathname === "/graph-fixture.js")
      return request.fulfill({ contentType: "text/javascript", body: script });
    if (url.pathname === "/graph-fixture.css")
      return request.fulfill({ contentType: "text/css", body: css });
    return request.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Graph fixture</title><link rel="stylesheet" href="/graph-fixture.css"></head><body><div id="root"></div><script>window.shellBoot=${JSON.stringify({ hold: [], fail: [], signedOut: false, ...boot })}</script><script src="/graph-fixture.js"></script></body></html>`,
    });
  });
  await page.goto(`http://127.0.0.1:5174${boot.path ?? `/${route}`}`);
  await expect(page.locator("main h1")).toHaveText(names[route].heading);
  return { page, context, errors };
}
async function settle(page) {
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
}
async function finish(page) {
  await page.evaluate(() => {
    const fixture = window.shellFixture;
    fixture.hold = [];
    while (fixture.pending.length) fixture.finish(fixture.pending[0].name);
  });
  await settle(page);
}
async function close(context, errors) {
  await context.close();
  expect(errors).toEqual([]);
}
function near(actual, expected, message) {
  expect(
    Math.abs(actual - expected),
    `${message}: ${actual} versus ${expected}`,
  ).toBeLessThanOrEqual(1);
}
async function geometry(page, route, state, insets = { left: 0, right: 0 }) {
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  const data = await page.evaluate(
    ({ hostSelector, viewportSelector }) => {
      const box = (element) => {
        const b = element.getBoundingClientRect();
        return {
          left: b.left,
          top: b.top,
          right: b.right,
          bottom: b.bottom,
          width: b.width,
          height: b.height,
        };
      };
      const main = document.querySelector("main");
      const host = document.querySelector(hostSelector);
      const viewport = document.querySelector(viewportSelector);
      const toolbar = host.querySelector(".od-graph-toolbar");
      const nav = document.querySelector(".od-application-mobile-navigation");
      const inspector = host.querySelector(".od-graph-inspector");
      const surface = main.querySelector(".od-page-surface");
      const computed = getComputedStyle(toolbar);
      return {
        main: box(main),
        host: box(host),
        viewport: box(viewport),
        toolbar: box(toolbar),
        surface: box(surface),
        nav: box(nav),
        sidebar: box(document.querySelector(".od-application-sidebar")),
        inspector: inspector ? box(inspector) : null,
        mode: inspector?.dataset.mode,
        paddingLeft: parseFloat(computed.paddingLeft),
        paddingRight: parseFloat(computed.paddingRight),
        gutter: parseFloat(
          getComputedStyle(document.documentElement).getPropertyValue(
            "--od-page-gutter",
          ),
        ),
        fontSize: parseFloat(
          getComputedStyle(document.documentElement).fontSize,
        ),
        viewportHeight: innerHeight,
        viewportWidth: innerWidth,
        scroll: {
          width: document.documentElement.scrollWidth,
          height: document.documentElement.scrollHeight,
          clientWidth: document.documentElement.clientWidth,
          clientHeight: document.documentElement.clientHeight,
        },
        surfaces: [main, surface, host, viewport].map((element) => {
          const c = getComputedStyle(element);
          return {
            maxWidth: c.maxWidth,
            marginLeft: c.marginLeft,
            marginRight: c.marginRight,
            borderLeft: c.borderLeftWidth,
            borderRight: c.borderRightWidth,
            radius: c.borderTopLeftRadius,
          };
        }),
      };
    },
    {
      hostSelector: names[route].host,
      viewportSelector: names[route].viewportSelector,
    },
  );
  measured.push({ route, state, ...data });
  const bottom = data.nav.height > 0 ? data.nav.top : data.viewportHeight;
  near(data.main.top, 0, "main block start");
  near(data.main.bottom, bottom, "main block end");
  near(data.main.height, bottom, "main block size");
  near(data.host.top, 0, "stage block start");
  near(data.host.bottom, bottom, "stage block end");
  near(data.host.left, data.main.left, "stage inline start");
  near(data.host.right, data.main.right, "stage inline end");
  near(data.surface.height, data.main.height, "page surface block size");
  near(data.viewport.top, data.toolbar.bottom, "viewport follows controls");
  near(data.viewport.bottom, data.host.bottom, "viewport block end");
  near(data.viewport.left, data.host.left, "viewport has no left gutter");
  near(
    data.viewport.right,
    data.mode === "split" ? data.inspector.left : data.host.right,
    "viewport right boundary",
  );
  near(
    data.toolbar.left,
    data.viewport.left,
    "toolbar region starts with graph",
  );
  near(
    data.toolbar.right,
    data.viewport.right,
    "toolbar region ends with graph",
  );
  // Resolve the shared gutter through a real computed length, including clamp/rem.
  const gutter = await page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.width = "var(--od-page-gutter)";
    probe.style.position = "fixed";
    document.body.append(probe);
    const width = probe.getBoundingClientRect().width;
    probe.remove();
    return width;
  });
  near(
    data.paddingLeft,
    Math.max(gutter, insets.left),
    "physical left control inset",
  );
  near(
    data.paddingRight,
    Math.max(gutter, insets.right),
    "physical right control inset",
  );
  expect(data.scroll.width).toBeLessThanOrEqual(data.scroll.clientWidth);
  expect(data.scroll.height).toBeLessThanOrEqual(data.scroll.clientHeight);
  for (const surface of data.surfaces) {
    expect(["none", "100%"].includes(surface.maxWidth)).toBe(true);
    expect(parseFloat(surface.marginLeft)).toBeGreaterThanOrEqual(0);
    expect(parseFloat(surface.marginRight)).toBeGreaterThanOrEqual(0);
    expect(parseFloat(surface.borderLeft)).toBe(0);
    expect(parseFloat(surface.borderRight)).toBe(0);
    expect(parseFloat(surface.radius)).toBe(0);
  }
  if (data.nav.height > 0) {
    near(data.main.left, 0, "phone left edge");
    near(data.main.right, data.viewportWidth, "phone right edge");
    near(data.nav.bottom, data.viewportHeight, "navigation dynamic block end");
  } else near(data.sidebar.right, data.main.left, "sidebar/main boundary");
  if (data.inspector) {
    const mode =
      data.host.width >= 69 * data.fontSize
        ? "split"
        : data.host.width > 48 * data.fontSize
          ? "overlay"
          : "sheet";
    expect(data.mode).toBe(mode);
    if (mode === "split") {
      near(data.inspector.width, 21 * data.fontSize, "split width");
      near(data.inspector.right, data.host.right, "split inline end");
      near(data.inspector.top, data.host.top, "split block start");
      near(data.inspector.bottom, data.host.bottom, "split block end");
      const border = await page
        .locator(".od-graph-inspector")
        .evaluate((e) => parseFloat(getComputedStyle(e).borderLeftWidth));
      expect(border).toBeLessThanOrEqual(1);
    } else if (mode === "overlay") {
      near(data.inspector.width, 21 * data.fontSize, "overlay width");
      near(
        data.host.right - data.inspector.right,
        0.875 * data.fontSize,
        "overlay inline end inset",
      );
      near(
        data.host.bottom - data.inspector.bottom,
        0.875 * data.fontSize,
        "overlay block end inset",
      );
      near(
        data.inspector.top - data.host.top,
        Math.max(
          4.75 * data.fontSize,
          data.toolbar.bottom - data.host.top + 0.875 * data.fontSize,
        ),
        "overlay below complete controls",
      );
    } else {
      near(data.inspector.left, 0.75 * data.fontSize, "sheet left inset");
      near(
        data.viewportWidth - data.inspector.right,
        0.75 * data.fontSize,
        "sheet right inset",
      );
      near(
        data.viewportHeight - data.inspector.bottom,
        0.75 * data.fontSize,
        "sheet bottom inset",
      );
      expect(data.inspector.height).toBeLessThanOrEqual(
        data.viewportHeight - 1.5 * data.fontSize + 1,
      );
    }
  }
  return data;
}
async function evidenceFor(page, route, name, insets) {
  await geometry(page, route, name, insets);
  const violations = (await new AxeBuilder({ page }).analyze()).violations;
  await page.screenshot({
    path: resolve(evidence, `${name}.png`),
    fullPage: true,
  });
  expect(violations).toEqual([]);
}
async function identity(page, route) {
  const info = names[route];
  const heading = page.getByRole("heading", {
    level: 1,
    name: info.heading,
    exact: true,
  });
  await expect(heading).toHaveCount(1);
  await expect(heading).toHaveAttribute("tabindex", "-1");
  await expect(
    page.getByRole("main", { name: info.heading, exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByRole("region", { name: info.viewport, exact: true }),
  ).toHaveCount(1);
  await expect(page).toHaveTitle(new RegExp(`^${info.heading}`));
  await expect(page.locator("main .od-page-heading")).toHaveCount(0);
  await expect(page.locator(".od-application-topbar")).toHaveCount(0);
  const h = await heading.evaluate((e) => ({
    position: getComputedStyle(e).position,
    hidden: e.hidden || e.getAttribute("aria-hidden") === "true",
    display: getComputedStyle(e).display,
    visibility: getComputedStyle(e).visibility,
    height: e.getBoundingClientRect().height,
  }));
  expect(h.position).toBe("absolute");
  expect(h.hidden).toBe(false);
  expect(h.display).not.toBe("none");
  expect(h.visibility).not.toBe("hidden");
  expect(h.height).toBeLessThanOrEqual(1);
  await expect(heading).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole(route === "services" ? "button" : "combobox", {
      name: info.first,
      exact: true,
    }),
  ).toBeFocused();
}
async function inspector(page, route) {
  const opener =
    route === "services"
      ? page.locator('[data-service-api-name="root"]')
      : page.locator(".od-relationship-graph-node").first();
  await opener.focus();
  await page.keyboard.press("Space");
  await expect(page.locator(".od-graph-inspector h2")).toBeFocused();
  return opener;
}
async function toolbarReachable(page, route) {
  const controls = page
    .locator(`${names[route].host} > .od-graph-toolbar`)
    .locator("button, input, select");
  for (const control of await controls.all()) {
    if (!(await control.isVisible())) continue;
    await control.focus();
    await expect(control).toBeFocused();
    expect(
      await control.evaluate((e) => {
        const b = e.getBoundingClientRect();
        const hit = document.elementFromPoint(
          b.left + b.width / 2,
          b.top + b.height / 2,
        );
        return hit === e || e.contains(hit);
      }),
    ).toBe(true);
  }
}
async function oversized(page, route) {
  await page.evaluate((route) => {
    const f = window.shellFixture;
    if (route === "services") {
      const root = f.values.services.items[0];
      const items = [root];
      for (let depth = 1; depth <= 10; depth++)
        for (let column = 0; column < 8; column++)
          items.push({
            ...root,
            api_name: `node-${depth}-${column}`,
            display_name: `Service ${depth}, ${column}`,
            is_root: false,
            parent_service_api_name:
              depth === 1 ? "root" : `node-${depth - 1}-${column}`,
          });
      f.values.services = { items, page: { has_more: false } };
    } else {
      const provider = f.values.providers.items[0];
      const model = f.values.models.items[0];
      f.values.providers = {
        items: Array.from({ length: 16 }, (_, i) => ({
          ...provider,
          api_name: `provider-${i}`,
          display_name: `Provider ${i}`,
        })),
        page: { has_more: false },
      };
      f.values.models = {
        items: Array.from({ length: 16 }, (_, i) => ({
          ...model,
          api_name: `model-${i}`,
          display_name: `Model ${i}`,
        })),
        page: { has_more: false },
      };
    }
  }, route);
  await page
    .getByRole("button", {
      name:
        names[route].first === "Service context"
          ? "Refresh configuration"
          : "Refresh services",
      exact: true,
    })
    .click();
  await settle(page);
  if (route === "configuration") {
    // A deliberately oversized board fixture tests viewport containment at each size.
    // Production records remain real App projections; no wrapper or toolbar rule changes.
    await page.locator(".od-relationship-graph-board").evaluate((e) => {
      e.style.minWidth = "1800px";
      e.style.minHeight = "1800px";
    });
  }
  const viewport = page.locator(names[route].viewportSelector);
  const dimensions = await viewport.evaluate((e) => ({
    x: e.scrollWidth - e.clientWidth,
    y: e.scrollHeight - e.clientHeight,
  }));
  expect(dimensions.x).toBeGreaterThan(0);
  expect(dimensions.y).toBeGreaterThan(0);
  const toolbar = page.locator(`${names[route].host} > .od-graph-toolbar`);
  const before = await toolbar.boundingBox();
  await viewport.evaluate((e) => {
    e.scrollLeft = 300;
    e.scrollTop = 300;
  });
  const position = await viewport.evaluate((e) => ({
    x: e.scrollLeft,
    y: e.scrollTop,
  }));
  expect(position.x).toBeGreaterThan(0);
  expect(position.y).toBeGreaterThan(0);
  expect(await toolbar.boundingBox()).toEqual(before);
  await viewport.evaluate((e) => {
    e.scrollLeft = 0;
    e.scrollTop = 0;
  });
  expect(await viewport.evaluate((e) => [e.scrollLeft, e.scrollTop])).toEqual([
    0, 0,
  ]);
}

describe("full-height edge-to-edge graph pages", () => {
  for (const [width, height] of sizes)
    for (const route of Object.keys(names)) {
      it(
        `${width} ${route}: normal, inspectors, overflow, text, and navigation`,
        { timeout: 60000 },
        async () => {
          const { page, context, errors } = await open(width, height, route);
          try {
            await settle(page);
            await identity(page, route);
            await evidenceFor(page, route, `${width}-${route}-normal`);
            const controls = page.locator(
              `${names[route].viewportSelector} button[tabindex="0"]`,
            );
            await expect(controls).toHaveCount(1);
            await controls.focus();
            await page.keyboard.press("Tab");
            expect(
              await page.evaluate(
                () =>
                  !document.activeElement.matches(
                    ".od-graph-node, .od-relationship-graph-node",
                  ),
              ),
            ).toBe(true);
            if (route === "services") {
              const center = await page
                .locator(".od-graph-canvas")
                .evaluate((e) => {
                  const b = e.getBoundingClientRect(),
                    v = e.parentElement.getBoundingClientRect();
                  return [
                    b.left + b.width / 2,
                    v.left + v.width / 2,
                    b.top + b.height / 2,
                    v.top + v.height / 2,
                  ];
                });
              near(center[0], center[1], "small tree horizontal center");
              near(center[2], center[3], "small tree vertical center");
            }
            const opener = await inspector(page, route);
            await evidenceFor(page, route, `${width}-${route}-inspector`);
            if (width === 390) {
              expect(
                await page
                  .locator(".od-graph-inspector")
                  .evaluate((e) => e.matches(":modal")),
              ).toBe(true);
              for (let i = 0; i < 18; i++) {
                await page.keyboard.press("Tab");
                expect(
                  await page.evaluate(
                    () =>
                      !!document.activeElement.closest(".od-graph-inspector"),
                  ),
                ).toBe(true);
              }
              await page
                .getByRole("button", { name: "Navigation", exact: true })
                .evaluate((e) => e.focus());
              expect(
                await page.evaluate(
                  () => !!document.activeElement.closest(".od-graph-inspector"),
                ),
              ).toBe(true);
              expect(
                await page.evaluate(
                  () =>
                    document
                      .elementFromPoint(1, 1)
                      ?.closest(".od-graph-inspector") !== null,
                ),
              ).toBe(true);
            } else await toolbarReachable(page, route);
            await page.locator(".od-graph-inspector h2").focus();
            await page.keyboard.press("Escape");
            await expect(page.locator(".od-graph-inspector")).toHaveCount(0);
            await expect(opener).toBeFocused();
            await oversized(page, route);
            await evidenceFor(page, route, `${width}-${route}-oversized`);
            await toolbarReachable(page, route);
            await page.evaluate(() => {
              document.documentElement.style.fontSize = "32px";
            });
            await evidenceFor(page, route, `${width}-${route}-text200`);
            await toolbarReachable(page, route);
            await inspector(page, route);
            await evidenceFor(
              page,
              route,
              `${width}-${route}-text200-inspector`,
            );
          } finally {
            await close(context, errors);
          }
        },
      );
      it(
        `${width} ${route}: loading, failure, retry, stale, and empty states`,
        { timeout: 60000 },
        async () => {
          const { page, context, errors } = await open(width, height, route, {
            hold: methods,
          });
          try {
            await identity(page, route);
            await evidenceFor(page, route, `${width}-${route}-loading`);
            await page.evaluate(() => {
              const f = window.shellFixture;
              f.hold = [];
              while (f.pending.length) f.finish(f.pending[0].name, true);
            });
            await settle(page);
            await evidenceFor(page, route, `${width}-${route}-failure`);
            const retry = page
              .getByRole("button", {
                name:
                  route === "services"
                    ? "Retry services"
                    : "Retry configuration",
                exact: true,
              })
              .first();
            await page.evaluate((methods) => {
              window.shellFixture.hold = methods;
            }, methods);
            await retry.focus();
            await page.keyboard.press("Enter");
            await expect(retry).toBeFocused();
            await expect(retry).toHaveAttribute("aria-busy", "true");
            const pendingCount = await page.evaluate(
              () => window.shellFixture.pending.length,
            );
            await page.keyboard.press("Enter");
            expect(
              await page.evaluate(() => window.shellFixture.pending.length),
            ).toBe(pendingCount);
            await evidenceFor(page, route, `${width}-${route}-retry-pending`);
            await finish(page);
            await evidenceFor(page, route, `${width}-${route}-retry`);
            await page.evaluate((methods) => {
              window.shellFixture.fail = methods;
            }, methods);
            const refresh = page.getByRole("button", {
              name: `Refresh ${route}`,
              exact: true,
            });
            await refresh.click();
            await settle(page);
            await expect(refresh).toBeFocused();
            await evidenceFor(page, route, `${width}-${route}-stale`);
            await expect(
              page.locator(
                `${names[route].viewportSelector} button[tabindex="0"]`,
              ),
            ).toHaveCount(1);
            await page.evaluate(() => {
              document.documentElement.style.fontSize = "32px";
            });
            await evidenceFor(page, route, `${width}-${route}-text200-stale`);
            const stateRetry = page
              .locator(names[route].viewportSelector)
              .getByRole("button", { name: new RegExp(`^Retry ${route}`) })
              .first();
            await stateRetry.focus();
            await expect(stateRetry).toBeFocused();
            await page.evaluate(() => {
              document.documentElement.style.fontSize = "16px";
              window.shellFixture.fail = [];
            });
            if (route === "configuration") {
              await page.evaluate(() => {
                for (const name of [
                  "providers",
                  "models",
                  "providerModels",
                  "credentials",
                ])
                  window.shellFixture.values[name] = {
                    items: [],
                    page: { has_more: false },
                  };
              });
              await refresh.click();
              await settle(page);
              await evidenceFor(page, route, `${width}-${route}-empty`);
              await expect(
                page
                  .locator(".od-relationship-graph-empty")
                  .getByRole("button", { name: "Add provider", exact: true }),
              ).toBeVisible();
              await page
                .locator(".od-relationship-graph-empty")
                .getByRole("button", { name: "Add provider", exact: true })
                .focus();
              await expect(
                page
                  .locator(".od-relationship-graph-empty")
                  .getByRole("button", { name: "Add provider", exact: true }),
              ).toBeFocused();
            } else {
              await inspector(page, route);
              await page.locator(".od-graph-inspector h2").focus();
              await page.keyboard.press("Escape");
              await page.evaluate(() => {
                const f = window.shellFixture;
                f.values.services.items = f.values.services.items.filter(
                  (s) => s.api_name !== "root",
                );
              });
              await refresh.click();
              await settle(page);
              await expect(
                page.locator('[data-service-api-name="root"]'),
              ).toHaveCount(1);
              await evidenceFor(
                page,
                route,
                `${width}-${route}-malformed-refresh`,
              );
            }
          } finally {
            await close(context, errors);
          }
        },
      );
    }
  for (const [width, height] of sizes) {
    it(
      `${width} configuration: partial board and assignment failure`,
      { timeout: 30000 },
      async () => {
        const { page, context, errors } = await open(
          width,
          height,
          "configuration",
          { hold: methods },
        );
        try {
          await page.evaluate(() => {
            window.shellFixture.values.providers.page.has_more = true;
          });
          await finish(page);
          await expect(
            page.getByText("Partial configuration graph", { exact: true }),
          ).toBeVisible();
          await evidenceFor(
            page,
            "configuration",
            `${width}-configuration-partial`,
          );
          const content = page.locator(".od-graph-viewport-content");
          const inset = await content.evaluate((e) => {
            const toolbar = document.querySelector(
              ".od-relationship-graph > .od-graph-toolbar",
            );
            const a = getComputedStyle(e),
              b = getComputedStyle(toolbar);
            return [
              parseFloat(a.paddingLeft),
              parseFloat(b.paddingLeft),
              parseFloat(a.paddingRight),
              parseFloat(b.paddingRight),
            ];
          });
          near(inset[0], inset[1], "one partial-state left inset");
          near(inset[2], inset[3], "one partial-state right inset");
          async function reachable(action) {
            await action.scrollIntoViewIfNeeded();
            await action.focus();
            await expect(action).toBeFocused();
            expect(
              await action.evaluate((e) => {
                const b = e.getBoundingClientRect();
                const hit = document.elementFromPoint(
                  b.left + b.width / 2,
                  b.top + b.height / 2,
                );
                return hit === e || e.contains(hit);
              }),
            ).toBe(true);
          }
          await reachable(
            page.getByRole("button", {
              name: "Load more Providers",
              exact: true,
            }),
          );
          await page.evaluate(() => {
            document.documentElement.style.fontSize = "32px";
          });
          await evidenceFor(
            page,
            "configuration",
            `${width}-configuration-partial-text200`,
          );
          await reachable(
            page.getByRole("button", {
              name: "Load more Providers",
              exact: true,
            }),
          );
          await page.evaluate(() => {
            document.documentElement.style.fontSize = "16px";
            window.shellFixture.fail = ["assignments"];
          });
          await page
            .getByRole("combobox", { name: "Service context", exact: true })
            .selectOption("root");
          await settle(page);
          await expect(
            page.getByText("Assignments are stale or unavailable.", {
              exact: true,
            }),
          ).toBeVisible();
          await evidenceFor(
            page,
            "configuration",
            `${width}-configuration-assignment-failure`,
          );
          await page.evaluate(() => {
            document.documentElement.style.fontSize = "32px";
          });
          await evidenceFor(
            page,
            "configuration",
            `${width}-configuration-assignment-failure-text200`,
          );
          await reachable(
            page.getByRole("button", {
              name: "Refresh configuration",
              exact: true,
            }),
          );
        } finally {
          await close(context, errors);
        }
      },
    );
  }
  for (const [width, height] of sizes) {
    it(
      `${width} Services rejects nonempty root-missing and empty initial responses`,
      { timeout: 30000 },
      async () => {
        for (const empty of [false, true]) {
          const { page, context, errors } = await open(
            width,
            height,
            "services",
            { hold: ["services"], path: "/services?service=child" },
          );
          try {
            await page.evaluate((empty) => {
              const f = window.shellFixture;
              f.values.services.items = empty
                ? []
                : f.values.services.items.filter((s) => s.api_name !== "root");
            }, empty);
            await finish(page);
            await expect(page.locator("[data-service-api-name]")).toHaveCount(
              0,
            );
            await expect(page.locator(".od-graph-inspector")).toHaveCount(0);
            await expect(
              page.getByRole("button", { name: "Create service", exact: true }),
            ).toHaveCount(0);
            await expect(
              page.getByRole("button", { name: "Retry services", exact: true }),
            ).toBeVisible();
            await evidenceFor(
              page,
              "services",
              `${width}-services-${empty ? "empty-invalid" : "root-missing"}`,
            );
          } finally {
            await close(context, errors);
          }
        }
      },
    );
  }
  for (const route of Object.keys(names)) {
    it(
      `phone ${route}: unequal physical safe areas and dynamic viewport`,
      { timeout: 30000 },
      async () => {
        const { page, context, errors } = await open(390, 844, route);
        try {
          await settle(page);
          const initialNavigation = await page
            .locator(".od-application-mobile-navigation")
            .boundingBox();
          const cdp = await context.newCDPSession(page);
          await cdp.send("Emulation.setSafeAreaInsetsOverride", {
            insets: { top: 0, left: 31, right: 47, bottom: 24 },
          });
          await evidenceFor(page, route, `390-${route}-safe-area`, {
            left: 31,
            right: 47,
          });
          const reserved = await page
            .locator(".od-application-mobile-navigation")
            .boundingBox();
          near(
            reserved.height,
            initialNavigation.height + 24,
            "exact bottom safe-area reservation",
          );
          await page.setViewportSize({ width: 390, height: 694 });
          await evidenceFor(page, route, `390-${route}-dynamic-resize`, {
            left: 31,
            right: 47,
          });
          await inspector(page, route);
          await evidenceFor(page, route, `390-${route}-safe-area-inspector`, {
            left: 31,
            right: 47,
          });
        } finally {
          await close(context, errors);
        }
      },
    );
  }
});
