// Run: node apps/admin/test/logs.browser.mjs
/* global window, document, innerWidth, getComputedStyle */
// The real App uses controlled client calls. No session or product data is used.
import { existsSync } from "node:fs";
import { mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { URL } from "node:url";
import { after, before, describe, it } from "node:test";

const root = resolve(import.meta.dirname, "../../..");
const requireShared = createRequire(
  resolve(
    process.env.OPENDLE_UI_PATH ?? resolve(root, "../opendle-ui"),
    "package.json",
  ),
);
const { chromium, expect } = requireShared("@playwright/test");
const { build } = requireShared("esbuild");
const { default: AxeBuilder } = requireShared("@axe-core/playwright");
const shared = dirname(
  createRequire(import.meta.url).resolve("@opendle/ui/package.json"),
);
const evidence = resolve(
  process.env.LLMROUTER_LOGS_EVIDENCE ?? "/tmp/llmrouter-logs-browser",
);
const origin = "http://127.0.0.1:5174";
const instant = "2026-09-09T12:34:56.789Z";
const automaticFrom = "2026-08-10T12:34:56Z";
const automaticTo = "2026-09-09T12:34:56Z";
let browser;
let script;
let css;

before(
  async () => {
    const bundle = await build({
      entryPoints: [resolve(root, "apps/admin/test/fixtures/logs-browser.tsx")],
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
  },
  { timeout: 30_000 },
);
after(async () => {
  await browser?.close();
});

async function open(width, boot = {}) {
  const context = await browser.newContext({
    viewport: { width, height: width === 390 ? 844 : 1000 },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  await page.clock.setFixedTime(new Date(instant));
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    expect(url.origin).toBe(origin);
    if (url.pathname === "/logs-fixture.js")
      await route.fulfill({ contentType: "text/javascript", body: script });
    else if (url.pathname === "/logs-fixture.css")
      await route.fulfill({ contentType: "text/css", body: css });
    else {
      expect(["/logs", "/overview", "/favicon.ico"]).toContain(url.pathname);
      await route.fulfill({
        contentType: "text/html",
        body: `<!doctype html><html lang="en"><head><title>Logs fixture</title><link rel="stylesheet" href="/logs-fixture.css"></head><body><div id="root"></div><script>window.logsBoot=${JSON.stringify({ hold: [], fail: [], ...boot })}</script><script src="/logs-fixture.js"></script></body></html>`,
      });
    }
  });
  await page.goto(`${origin}/logs`);
  await expect(
    page.getByRole("heading", { name: "Logs", exact: true }),
  ).toBeVisible();
  return { context, page, errors };
}
async function close({ context, errors }) {
  await context.close();
  expect(errors).toEqual([]);
}
const action = (page, name) =>
  page.getByRole("button", { name, exact: true }).filter({ visible: true });
const row = (page, id) =>
  action(page, `Inspect Logs details for request ${id}`);
const details = (page) =>
  page.getByRole("region", { name: "Logs details", exact: true });
const text = (page, value) =>
  page.getByText(value, { exact: false }).filter({ visible: true }).first();
async function state(page, value) {
  await expect(text(page, value)).toBeVisible();
  expect(
    await page
      .locator(
        '[aria-live="polite"], [aria-live="assertive"], [role="status"], [role="alert"]',
      )
      .filter({ hasText: value })
      .count(),
  ).toBeGreaterThan(0);
}
async function proof(page, name) {
  const filterGeometry = await page
    .getByRole("form", { name: "Logs filters", exact: true })
    .evaluate((form) => {
      const container = form.parentElement;
      if (!container) throw Error("Missing filter container.");
      const style = getComputedStyle(container);
      return {
        width: form.getBoundingClientRect().width,
        available:
          container.clientWidth -
          Number.parseFloat(style.paddingLeft) -
          Number.parseFloat(style.paddingRight),
      };
    });
  expect(filterGeometry.width).toBeCloseTo(filterGeometry.available, 0);
  const rowGeometry = await page
    .getByRole("button", { name: /^Inspect Logs details for request / })
    .filter({ visible: true })
    .evaluateAll((buttons) =>
      buttons.map((button) => {
        const style = getComputedStyle(button);
        const canvas = document.createElement("canvas");
        const context = canvas.getContext("2d");
        context.font = style.font;
        return {
          available:
            button.clientWidth -
            Number.parseFloat(style.paddingLeft) -
            Number.parseFloat(style.paddingRight),
          wordWidth: context.measureText("Inspect").width,
        };
      }),
    );
  for (const geometry of rowGeometry)
    expect(geometry.available).toBeGreaterThanOrEqual(geometry.wordWidth);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: resolve(evidence, `${name}.png`),
    fullPage: true,
  });
}
async function patch(page, value) {
  await page.evaluate(
    (update) => Object.assign(window.logsFixture, update),
    value,
  );
}
async function value(page, name, next) {
  await page.evaluate(
    ({ name, next }) => {
      window.logsFixture.values[name] = next;
    },
    { name, next },
  );
}
async function calls(page, name) {
  return page.evaluate(
    (method) =>
      window.logsFixture.calls
        .filter((call) => call.name === method)
        .map((call) => call.args),
    name,
  );
}
async function finish(page, name, next, fail = false, index = 0) {
  await page.evaluate(
    ({ name, next, fail, index }) =>
      window.logsFixture.finish(name, next, fail, index),
    { name, next, fail, index },
  );
}
async function pending(page, name, count = 1) {
  await expect
    .poll(() =>
      page.evaluate(
        (method) =>
          window.logsFixture.pending.filter(
            (operation) => operation.name === method,
          ).length,
        name,
      ),
    )
    .toBe(count);
}
async function pageValue(page, ids, metadata) {
  await page.evaluate(
    ({ ids, metadata }) => {
      window.logsFixture.values.requestLogsPage = {
        items: ids.map(window.logsFixture.summary),
        page: metadata,
      };
    },
    { ids, metadata },
  );
}
async function finishPage(page, ids, metadata, index = 0) {
  await page.evaluate(
    ({ ids, metadata, index }) =>
      window.logsFixture.finish(
        "requestLogsPage",
        { items: ids.map(window.logsFixture.summary), page: metadata },
        false,
        index,
      ),
    { ids, metadata, index },
  );
}
async function clear(page) {
  await action(page, "Clear Logs filters").click();
  await state(page, "Logs loaded: 2 records.");
}
async function refresh(page) {
  await action(page, "Refresh Logs").click();
  await state(page, "Logs loaded: 2 records.");
}

describe("automatic Logs browser behavior", () => {
  for (const width of [1440, 390]) {
    it(
      `covers entry, exact states, retry, and automatic bounds at ${width}`,
      { timeout: 90_000 },
      async () => {
        const test = await open(width, {
          hold: ["retention", "requestLogsPage"],
        });
        const { page } = test;
        try {
          await state(page, "Loading Logs.");
          await expect(
            page.getByRole("region", { name: "Logs", exact: true }),
          ).toBeVisible();
          await expect(
            page.getByRole("search", { name: "Logs filters", exact: true }),
          ).toBeVisible();
          expect(await calls(page, "requestLogsPage")).toEqual([]);
          await expect(
            page.getByText(
              "No Logs are available in the configured retention window.",
              { exact: true },
            ),
          ).toHaveCount(0);
          await proof(page, `${width}-entry-loading`);
          await finish(page, "retention", undefined, true);
          await state(page, "Logs are unavailable.");
          expect(await calls(page, "requestLogsPage")).toEqual([]);
          await proof(page, `${width}-retention-error`);
          await action(page, "Retry Logs").click();
          await pending(page, "retention");
          await finish(page, "retention");
          await pending(page, "requestLogsPage");
          const [initial] = await calls(page, "requestLogsPage");
          expect(initial.slice(0, 3)).toEqual([
            automaticFrom,
            automaticTo,
            undefined,
          ]);
          expect(initial[3] ?? {}).toEqual({});
          await finishPage(page, [], { has_more: false });
          await state(
            page,
            "No Logs are available in the configured retention window.",
          );
          await proof(page, `${width}-empty`);
          await patch(page, { hold: ["requestLogsPage"] });
          await action(page, "Refresh Logs").click();
          await pending(page, "requestLogsPage");
          await finish(page, "requestLogsPage", undefined, true);
          await state(page, "Logs are unavailable.");
          await proof(page, `${width}-first-page-error`);
          await patch(page, { hold: [] });
          await action(page, "Retry Logs").click();
          await state(page, "Logs loaded: 2 records.");
          await state(page, "More Logs are available.");
          await proof(page, `${width}-ready`);
          await pageValue(page, ["log-c"], {
            has_more: false,
            next_cursor: "ignored",
          });
          await action(page, "Refresh Logs").click();
          await state(page, "Logs loaded: 1 record.");
          await state(page, "All Logs in this range are loaded.");
          await expect(action(page, "Load more Logs")).toHaveCount(0);
          await proof(page, `${width}-one-record`);
        } finally {
          await close(test);
        }
      },
    );

    it(
      `covers all filters, exact queries, errors, and retained bounds at ${width}`,
      { timeout: 120_000 },
      async () => {
        const test = await open(width);
        const { page } = test;
        try {
          await state(page, "Logs loaded: 2 records.");
          const filters = page.getByRole("search", {
            name: "Logs filters",
            exact: true,
          });
          const names = [
            "From time (UTC)",
            "Before time (UTC)",
            "Call actor",
            "Administrator",
            "Assignment configuration service",
          ];
          const inputs = names.map((name) =>
            filters.getByLabel(name, { exact: true }),
          );
          for (const input of inputs) await expect(input).toBeVisible();
          expect(await inputs[2].locator("option").allTextContents()).toEqual([
            "All call actors",
            "Service calls",
            "Administrator playground calls",
          ]);
          await inputs[0].focus();
          for (const input of inputs) {
            await expect(input).toBeFocused();
            await page.keyboard.press("Tab");
          }
          await expect(action(page, "Apply Logs filters")).toBeFocused();
          await inputs[0].fill("2026-09-01T01:02:03");
          await inputs[1].fill("2026-09-09T01:02:03");
          await inputs[2].selectOption("administrator");
          await inputs[3].fill("fixture-subject");
          await inputs[4].fill("fixture-service");
          await action(page, "Apply Logs filters").click();
          await state(page, "Logs loaded: 2 records.");
          await expect(action(page, "Apply Logs filters")).toBeFocused();
          expect((await calls(page, "requestLogsPage")).at(-1)).toEqual([
            "2026-09-01T01:02:03Z",
            "2026-09-09T01:02:03Z",
            undefined,
            {
              call_actor: "administrator",
              administrator: "fixture-subject",
              configuration_service: "fixture-service",
            },
          ]);
          expect((await calls(page, "retention")).length).toBe(1);
          await proof(page, `${width}-active-filters`);
          await patch(page, { hold: ["requestLogsPage"] });
          await action(page, "Load more Logs").click();
          await pending(page, "requestLogsPage");
          expect((await calls(page, "requestLogsPage")).at(-1)).toEqual([
            "2026-09-01T01:02:03Z",
            "2026-09-09T01:02:03Z",
            "cursor-1",
            {
              call_actor: "administrator",
              administrator: "fixture-subject",
              configuration_service: "fixture-service",
            },
          ]);
          await finishPage(page, ["log-a"], {
            has_more: true,
            next_cursor: "cursor-2",
          });
          await state(page, "Logs loaded: 3 records.");
          await patch(page, { hold: [] });
          await row(page, "log-c").click();
          await expect(
            details(page).getByRole("heading", {
              name: "Logs details for request log-c",
              exact: true,
            }),
          ).toBeFocused();
          const invalidCases = [
            [0, "2026-02-30T01:02:03", "Enter a valid From time in UTC."],
            [1, "2026-02-30T01:02:03", "Enter a valid Before time in UTC."],
            [1, "2026-08-31T01:02:03", "From time must be before Before time."],
            [
              0,
              "2026-08-01T01:02:03",
              "Select times inside the configured Logs retention window.",
            ],
            [2, "invalid", "Select a valid call actor."],
            [
              3,
              "s".repeat(501),
              "Enter an administrator subject of 500 characters or fewer.",
            ],
            [
              4,
              "Invalid_Name",
              "Enter a valid assignment configuration service API name.",
            ],
          ];
          const valid = [
            "2026-09-01T01:02:03",
            "2026-09-09T01:02:03",
            "administrator",
            "fixture-subject",
            "fixture-service",
          ];
          for (const [index, invalid, error] of invalidCases) {
            const input = inputs[index];
            if (index === 2) {
              await input.evaluate((select) => {
                const option = document.createElement("option");
                option.value = "invalid";
                option.textContent = "Invalid fixture actor";
                select.append(option);
              });
              await input.selectOption(invalid);
            } else {
              // Date controls and maxlength must not conceal the host validation branch.
              await input.evaluate((element) => {
                element.type = "text";
                element.removeAttribute("maxlength");
              });
              await input.fill(invalid);
            }
            const before = (await calls(page, "requestLogsPage")).length;
            await action(page, "Apply Logs filters").click();
            await expect(input).toBeFocused();
            await expect(input).toHaveAttribute("aria-invalid", "true");
            const described = await input.getAttribute("aria-describedby");
            expect(described).toBeTruthy();
            expect(
              await page.evaluate(
                (ids) =>
                  ids
                    .split(/\s+/)
                    .map((id) => document.getElementById(id)?.textContent ?? "")
                    .join(" "),
                described,
              ),
            ).toContain(error);
            await state(page, error);
            expect((await calls(page, "requestLogsPage")).length).toBe(before);
            await expect(details(page)).toBeVisible();
            await expect(row(page, "log-c")).toBeVisible();
            await proof(
              page,
              `${width}-filter-error-${index}-${invalidCases.indexOf(invalidCases.find((item) => item[2] === error))}`,
            );
            if (index === 2) await input.selectOption(valid[index]);
            else await input.fill(valid[index]);
            await expect(input).not.toHaveAttribute("aria-invalid", "true");
          }
          await value(page, "retention", { duration_days: 1 });
          const before = (await calls(page, "requestLogsPage")).length;
          await action(page, "Refresh Logs").click();
          await state(
            page,
            "Select times inside the configured Logs retention window.",
          );
          await expect(inputs[0]).toBeFocused();
          await expect(inputs[0]).toHaveValue(valid[0]);
          await expect(details(page)).toHaveCount(0);
          await expect(row(page, "log-c")).toBeVisible();
          expect((await calls(page, "requestLogsPage")).length).toBe(before);
          await proof(page, `${width}-changed-retention-invalid`);
          await clear(page);
          for (const input of inputs) await expect(input).toHaveValue("");
          expect(
            (await calls(page, "requestLogsPage")).at(-1).slice(0, 3),
          ).toEqual(["2026-09-08T12:34:56Z", automaticTo, undefined]);
          expect(
            (await calls(page, "requestLogsPage")).at(-1)[3] ?? {},
          ).toEqual({});
          expect((await calls(page, "retention")).length).toBe(2);
          await inputs[2].selectOption("service");
          await pageValue(page, [], { has_more: false });
          await action(page, "Apply Logs filters").click();
          await state(page, "No Logs match these filters.");
          expect((await calls(page, "requestLogsPage")).at(-1)[3]).toEqual({
            call_actor: "service",
          });
          await proof(page, `${width}-filtered-empty`);
          await patch(page, { fail: ["requestLogsPage"] });
          await action(page, "Apply Logs filters").click();
          await state(page, "Logs are unavailable.");
          await expect(inputs[2]).toHaveValue("service");
          await proof(page, `${width}-filtered-error`);
        } finally {
          await close(test);
        }
      },
    );

    it(
      `keeps cursor, deduplication, retry, and focus rules at ${width}`,
      { timeout: 120_000 },
      async () => {
        const test = await open(width);
        const { page } = test;
        try {
          await state(page, "Logs loaded: 2 records.");
          await patch(page, { hold: ["requestLogsPage"] });
          await action(page, "Load more Logs").click();
          await pending(page, "requestLogsPage");
          await state(page, "Loading more Logs.");
          await action(page, "Loading more Logs.").press("Enter");
          expect((await calls(page, "requestLogsPage")).length).toBe(2);
          await proof(page, `${width}-more-loading`);
          await finish(page, "requestLogsPage", undefined, true);
          await state(page, "More Logs are unavailable.");
          await expect(action(page, "Retry loading more Logs")).toBeFocused();
          await expect(row(page, "log-c")).toBeVisible();
          await proof(page, `${width}-more-error`);
          await action(page, "Retry loading more Logs").click();
          await pending(page, "requestLogsPage");
          const requests = await calls(page, "requestLogsPage");
          expect(requests.at(-1)).toEqual(requests.at(-2));
          expect(requests.at(-1).slice(0, 3)).toEqual([
            automaticFrom,
            automaticTo,
            "cursor-1",
          ]);
          await page.evaluate(() => {
            const fixture = window.logsFixture;
            fixture.finish("requestLogsPage", {
              items: [
                {
                  ...fixture.summary("log-b"),
                  tags: ["duplicate-must-not-replace"],
                },
                fixture.summary("log-a"),
              ],
              page: { has_more: true, next_cursor: "cursor-2" },
            });
          });
          await state(page, "Logs loaded: 3 records.");
          await expect(action(page, "Load more Logs")).toBeFocused();
          expect(
            await page
              .getByRole("button", {
                name: /^Inspect Logs details for request /,
              })
              .filter({ visible: true })
              .evaluateAll((buttons) =>
                buttons.map((button) => button.getAttribute("aria-label")),
              ),
          ).toEqual([
            "Inspect Logs details for request log-c",
            "Inspect Logs details for request log-b",
            "Inspect Logs details for request log-a",
          ]);
          await expect(
            page.getByText("duplicate-must-not-replace", { exact: true }),
          ).toHaveCount(0);
          await action(page, "Load more Logs").click();
          await finishPage(page, ["log-0"], {
            has_more: false,
            next_cursor: "ignored",
          });
          await state(page, "Logs loaded: 4 records.");
          await state(page, "All Logs in this range are loaded.");
          expect(
            await page.evaluate(() => document.activeElement?.textContent),
          ).toContain("Logs loaded: 4 records.");
          await expect(action(page, "Load more Logs")).toHaveCount(0);
          await proof(page, `${width}-more-complete`);
          for (const [name, metadata, ids] of [
            [
              "repeated",
              { has_more: true, next_cursor: "cursor-1" },
              ["log-a"],
            ],
            ["missing", { has_more: true }, ["log-a"]],
            [
              "invalid",
              { has_more: true, next_cursor: "x".repeat(501) },
              ["log-a"],
            ],
            [
              "no-progress",
              { has_more: true, next_cursor: "cursor-2" },
              ["log-c", "log-b"],
            ],
          ]) {
            await patch(page, { hold: [] });
            await refresh(page);
            await patch(page, { hold: ["requestLogsPage"] });
            await action(page, "Load more Logs").click();
            await finishPage(page, ids, metadata);
            await state(page, "More Logs are unavailable.");
            await expect(action(page, "Refresh Logs")).toBeFocused();
            await expect(action(page, "Load more Logs")).toHaveCount(0);
            await expect(action(page, "Retry loading more Logs")).toHaveCount(
              0,
            );
            await expect(row(page, "log-c")).toBeVisible();
            await proof(page, `${width}-cursor-${name}`);
          }
        } finally {
          await close(test);
        }
      },
    );

    it(
      `keeps detail selection, keyboard focus, safe content, and media expiry at ${width}`,
      { timeout: 120_000 },
      async () => {
        const test = await open(width);
        const { page } = test;
        try {
          await state(page, "Logs loaded: 2 records.");
          await patch(page, { hold: ["requestLog"] });
          await row(page, "log-c").focus();
          await row(page, "log-c").press("Enter");
          const heading = details(page).getByRole("heading", {
            name: "Logs details for request log-c",
            exact: true,
          });
          await expect(heading).toBeFocused();
          await state(page, "Loading Logs details.");
          await proof(page, `${width}-detail-loading`);
          await finish(page, "requestLog", undefined, true);
          await state(page, "Logs details are unavailable.");
          await expect(action(page, "Retry Logs details")).toBeVisible();
          await expect(action(page, "Close Logs details")).toBeVisible();
          await proof(page, `${width}-detail-unavailable`);
          await action(page, "Retry Logs details").click();
          await pending(page, "requestLog");
          await finish(page, "requestLog");
          await expect(details(page)).toContainText("Complete detail log-c");
          expect(
            (await calls(page, "requestLog")).map((args) => args[0]),
          ).toEqual(["log-c", "log-c"]);
          await expect(
            details(page).locator("script, iframe, img, video, audio"),
          ).toHaveCount(0);
          await expect(details(page).locator('a[href^="https:"]')).toHaveCount(
            0,
          );
          expect(
            await page.evaluate(() => window.logMarkupExecuted),
          ).toBeUndefined();
          const scroll = await details(page).evaluate((region) => {
            const elements = [region, ...region.querySelectorAll("*")];
            const candidates = elements.filter((element) => {
              const style = getComputedStyle(element);
              return (
                /auto|scroll/.test(style.overflowY) &&
                element.scrollHeight > element.clientHeight + 1
              );
            });
            if (candidates.length !== 1) return false;
            const candidate = candidates[0];
            candidate.scrollTop = 80;
            return (
              candidate.classList.contains("log-detail") &&
              candidate.scrollTop > 0
            );
          });
          expect(scroll).toBe(true);
          const body = details(page).getByRole("region", {
            name: "Logs detail content",
            exact: true,
          });
          const bodyBox = await body.boundingBox();
          expect(bodyBox.height).toBeLessThanOrEqual(
            Math.min((width === 390 ? 844 : 1000) * 0.65, 768) + 1,
          );
          await body.focus();
          await body.evaluate((element) => {
            element.scrollTop = 0;
          });
          const headingTop = await heading.evaluate(
            (element) => element.getBoundingClientRect().top,
          );
          await body.press("PageDown");
          await expect
            .poll(() => body.evaluate((element) => element.scrollTop))
            .toBeGreaterThan(0);
          expect(
            await heading.evaluate(
              (element) => element.getBoundingClientRect().top,
            ),
          ).toBeCloseTo(headingTop, 0);
          await row(page, "log-c").click();
          await expect(heading).toBeFocused();
          await finish(page, "requestLog");
          await expect(details(page)).toContainText("Complete detail log-c");
          await proof(page, `${width}-detail-ready-long-content`);
          await action(page, "Close Logs details").click();
          await expect(details(page)).toHaveCount(0);
          await expect(row(page, "log-c")).toBeFocused();
          await row(page, "log-c").press("Space");
          await expect(heading).toBeFocused();
          await page.keyboard.press("Escape");
          await expect(row(page, "log-c")).toBeFocused();
          await finish(page, "requestLog");
          await expect(details(page)).toHaveCount(0);
          await patch(page, { hold: [] });
          await row(page, "log-b").click();
          await expect(details(page)).toContainText("Complete detail log-b");
          await patch(page, { fail: ["requestLogMedia"] });
          await action(page, "Prepare retained media download").click();
          await state(page, "Logs details are unavailable.");
          await expect(
            details(page).getByRole("heading", {
              name: "Logs details for request log-b",
              exact: true,
            }),
          ).toBeVisible();
          expect(await calls(page, "requestLogMedia")).toEqual([
            ["log-b", "media-fixture"],
          ]);
          await expect(row(page, "log-c")).toBeVisible();
          await proof(page, `${width}-media-expired`);
          await patch(page, { fail: [] });
          await action(page, "Retry Logs details").click();
          await expect(details(page)).toContainText("Complete detail log-b");
          await action(page, "Prepare retained media download").click();
          const download = details(page).getByRole("link", {
            name: "Download retained media",
            exact: true,
          });
          await expect(download).toHaveAttribute(
            "href",
            /^blob:http:\/\/127\.0\.0\.1:5174\//,
          );
          await expect(download).toHaveAttribute("download", /media-fixture/);
          await proof(page, `${width}-media-ready`);
          await action(page, "Refresh Logs").click();
          await expect(details(page)).toHaveCount(0);
          await expect(action(page, "Refresh Logs")).toBeFocused();
          await row(page, "log-c").click();
          await expect(details(page)).toContainText("Complete detail log-c");
          await row(page, "log-c").evaluate((button) => button.remove());
          await action(page, "Close Logs details").click();
          await expect(details(page)).toHaveCount(0);
          await expect(action(page, "Refresh Logs")).toBeFocused();
        } finally {
          await close(test);
        }
      },
    );

    it(
      `rejects stale list and detail responses without stealing focus at ${width}`,
      { timeout: 120_000 },
      async () => {
        const test = await open(width);
        const { page } = test;
        try {
          await state(page, "Logs loaded: 2 records.");
          await patch(page, {
            hold: ["requestLog", "requestLogsPage", "retention"],
          });
          await row(page, "log-c").click();
          await row(page, "log-b").click();
          await pending(page, "requestLog", 2);
          await finish(page, "requestLog", undefined, false, 1);
          await expect(details(page)).toContainText("Complete detail log-b");
          await finish(page, "requestLog", undefined, true);
          await expect(details(page)).toContainText("Complete detail log-b");
          await expect(
            details(page).getByRole("heading", {
              name: "Logs details for request log-b",
              exact: true,
            }),
          ).toBeFocused();
          await action(page, "Load more Logs").click();
          await pending(page, "requestLogsPage");
          await row(page, "log-c").click();
          await finish(page, "requestLog");
          await expect(details(page)).toContainText("Complete detail log-c");
          await finishPage(page, ["log-a"], {
            has_more: true,
            next_cursor: "cursor-2",
          });
          await expect(details(page)).toContainText("Complete detail log-c");
          await state(page, "Logs loaded: 3 records.");
          await action(page, "Load more Logs").click();
          await row(page, "log-b").click();
          await action(page, "Refresh Logs").click();
          await expect(details(page)).toHaveCount(0);
          await pending(page, "retention");
          const retentionCount = (await calls(page, "retention")).length;
          await action(page, "Refresh Logs").press("Enter");
          expect((await calls(page, "retention")).length).toBe(retentionCount);
          await finish(page, "requestLog", undefined, true);
          await finish(page, "requestLogsPage", undefined, true);
          await state(page, "Loading Logs.");
          await expect(action(page, "Refresh Logs")).toBeFocused();
          await finish(page, "retention");
          await pending(page, "requestLogsPage");
          await finishPage(page, ["new-log"], { has_more: false });
          await state(page, "Logs loaded: 1 record.");
          await expect(row(page, "log-c")).toHaveCount(0);
          await expect(row(page, "new-log")).toBeVisible();
          await expect(action(page, "Refresh Logs")).toBeFocused();
          await proof(page, `${width}-stale-responses-rejected`);
          // A newer filter sequence owns both the result and focus.
          await action(page, "Apply Logs filters").click();
          await pending(page, "requestLogsPage");
          await state(page, "Loading Logs.");
          await expect(action(page, "Apply Logs filters")).toBeFocused();
          await proof(page, `${width}-valid-filter-loading`);
          await action(page, "Clear Logs filters").click();
          await pending(page, "requestLogsPage", 2);
          await finishPage(page, ["current-log"], { has_more: false }, 1);
          await finishPage(page, ["obsolete-log"], {
            has_more: true,
            next_cursor: "obsolete",
          });
          await expect(row(page, "current-log")).toBeVisible();
          await expect(row(page, "obsolete-log")).toHaveCount(0);
          await expect(action(page, "Clear Logs filters")).toBeFocused();
          await proof(page, `${width}-stale-first-page-rejected`);
        } finally {
          await close(test);
        }
      },
    );
    it(
      `restores a valid walk after invalid actions and keeps pending details current at ${width}`,
      { timeout: 90_000 },
      async () => {
        const test = await open(width);
        const { page } = test;
        try {
          await state(page, "Logs loaded: 2 records.");
          await patch(page, { hold: ["requestLog", "requestLogsPage"] });
          await row(page, "log-c").click();
          await row(page, "log-c").press("Enter");
          await pending(page, "requestLog");
          expect(await calls(page, "requestLog")).toEqual([["log-c"]]);
          await action(page, "Load more Logs").click();
          await finishPage(page, ["log-a"], {
            has_more: true,
            next_cursor: "cursor-2",
          });
          await state(page, "Loading Logs details.");
          await finish(page, "requestLog");
          await expect(details(page)).toContainText("Complete detail log-c");
          await action(page, "Load more Logs").click();
          const administrator = page
            .getByRole("search", { name: "Logs filters", exact: true })
            .getByLabel("Administrator", { exact: true });
          await administrator.evaluate((input) =>
            input.removeAttribute("maxlength"),
          );
          await administrator.fill("s".repeat(501));
          const before = (await calls(page, "requestLogsPage")).length;
          await action(page, "Apply Logs filters").click();
          await state(
            page,
            "Enter an administrator subject of 500 characters or fewer.",
          );
          await expect(administrator).toBeFocused();
          await expect(details(page)).toContainText("Complete detail log-c");
          expect((await calls(page, "requestLogsPage")).length).toBe(before);
          await finishPage(page, ["obsolete-after-invalid"], {
            has_more: false,
          });
          await expect(row(page, "obsolete-after-invalid")).toHaveCount(0);
          await expect(administrator).toBeFocused();
          await expect(details(page)).toContainText("Complete detail log-c");
          await administrator.fill("");
          await action(page, "Load more Logs").click();
          await pending(page, "requestLogsPage");
          expect((await calls(page, "requestLogsPage")).at(-1)[2]).toBe(
            "cursor-2",
          );
          await finishPage(page, ["log-0"], { has_more: false });
          await state(page, "Logs loaded: 4 records.");
          await expect(details(page)).toContainText("Complete detail log-c");
          await proof(page, `${width}-invalid-action-restored-walk`);
          await patch(page, { hold: ["retention"] });
          await page.clock.setFixedTime(new Date("2026-09-09T12:34:57.999Z"));
          await action(page, "Refresh Logs").click();
          await expect(details(page)).toHaveCount(0);
          await state(page, "Loading Logs.");
          await proof(page, `${width}-refresh-loading`);
          await finish(page, "retention", { duration_days: 1 });
          await state(page, "Logs loaded: 2 records.");
          expect(
            (await calls(page, "requestLogsPage")).at(-1).slice(0, 2),
          ).toEqual(["2026-09-08T12:34:57Z", "2026-09-09T12:34:57Z"]);
          await expect(action(page, "Refresh Logs")).toBeFocused();
        } finally {
          await close(test);
        }
      },
    );
    it(
      `keeps mixed and short detail bodies usable at 200 percent text at ${width}`,
      { timeout: 90_000 },
      async () => {
        const test = await open(width, { hold: ["requestLog"] });
        const { page } = test;
        try {
          await state(page, "Logs loaded: 2 records.");
          await row(page, "log-c").click();
          await page.evaluate(() => {
            const record = window.logsFixture.detail("log-c");
            record.summary.tags = ["Long tag ".repeat(100), "W".repeat(500)];
            record.attempts = Array.from({ length: 20 }, () => ({
              ...record.attempts[0],
              error: {
                code: "provider_error",
                message: "Long error ".repeat(100),
              },
            }));
            window.logsFixture.finish("requestLog", record);
          });
          const body = details(page).getByRole("region", {
            name: "Logs detail content",
            exact: true,
          });
          await expect(body).toBeVisible();
          // Double each detail font once, without compounded inheritance.
          await details(page).evaluate((element) => {
            const elements = [element, ...element.querySelectorAll("*")];
            const sizes = elements.map(
              (item) => getComputedStyle(item).fontSize,
            );
            elements.forEach((item, index) => {
              item.style.fontSize = `${Number.parseFloat(sizes[index]) * 2}px`;
            });
          });
          await expect(body).toHaveAttribute("tabindex", "0");
          expect(await body.locator("pre[tabindex]").count()).toBe(0);
          const metrics = await body.evaluate((element) => ({
            height: element.getBoundingClientRect().height,
            overflow: element.scrollHeight > element.clientHeight,
            nested: [...element.querySelectorAll("*")].filter(
              (item) =>
                /auto|scroll/.test(getComputedStyle(item).overflowY) &&
                item.scrollHeight > item.clientHeight + 1,
            ).length,
          }));
          expect(metrics.height).toBeLessThanOrEqual(
            (width === 390 ? 844 : 1000) * 0.65 + 1,
          );
          expect(metrics.overflow).toBe(true);
          expect(metrics.nested).toBe(0);
          const closeButton = action(page, "Close Logs details");
          await closeButton.focus();
          await page.keyboard.press("Tab");
          await expect(body).toBeFocused();
          const heading = details(page).getByRole("heading", {
            name: "Logs details for request log-c",
            exact: true,
          });
          const positions = await Promise.all(
            [heading, closeButton].map((item) =>
              item.evaluate((element) => element.getBoundingClientRect().top),
            ),
          );
          await body.press("PageDown");
          await expect
            .poll(() => body.evaluate((element) => element.scrollTop))
            .toBeGreaterThan(0);
          for (const [index, item] of [heading, closeButton].entries())
            expect(
              await item.evaluate(
                (element) => element.getBoundingClientRect().top,
              ),
            ).toBeCloseTo(positions[index], 0);
          await proof(page, `${width}-detail-200-mixed`);
          await page.keyboard.press("Shift+Tab");
          await expect(closeButton).toBeFocused();
          await closeButton.press("Enter");
          await expect(row(page, "log-c")).toBeFocused();
          await row(page, "log-c").click();
          await page.evaluate(() => {
            const record = window.logsFixture.detail("log-c");
            record.request_json = "Short request";
            record.response_json = "Short response";
            record.attempts = [];
            record.media = [];
            window.logsFixture.finish("requestLog", record);
          });
          await expect(body).toContainText("Short response");
          await details(page).evaluate((element) => {
            const elements = [element, ...element.querySelectorAll("*")];
            const sizes = elements.map(
              (item) => getComputedStyle(item).fontSize,
            );
            elements.forEach((item, index) => {
              item.style.fontSize = `${Number.parseFloat(sizes[index]) * 2}px`;
            });
          });
          expect(
            await body.evaluate(
              (element) => element.getBoundingClientRect().height,
            ),
          ).toBeLessThanOrEqual((width === 390 ? 844 : 1000) * 0.65 + 1);
          await proof(page, `${width}-detail-200-short`);
          await action(page, "Retry Logs details").click();
          await finish(page, "requestLog", undefined, true);
          await state(page, "Logs details are unavailable.");
          await expect(body).toHaveCount(0);
          await proof(page, `${width}-detail-200-error`);
          await closeButton.click();
          await expect(row(page, "log-c")).toBeFocused();
        } finally {
          await close(test);
        }
      },
    );
    it(
      `loads the real App after StrictMode effect replay at ${width}`,
      { timeout: 30_000 },
      async () => {
        const test = await open(width, { strict: true, hold: ["retention"] });
        const { page } = test;
        try {
          await state(page, "Loading Logs.");
          await pending(page, "retention", 2);
          expect(await calls(page, "requestLogsPage")).toEqual([]);
          await finish(page, "retention", { duration_days: 1 });
          expect(await calls(page, "requestLogsPage")).toEqual([]);
          await state(page, "Loading Logs.");
          await finish(page, "retention");
          await state(page, "Logs loaded: 2 records.");
          expect((await calls(page, "requestLogsPage")).length).toBe(1);
          expect((await calls(page, "requestLogsPage"))[0].slice(0, 2)).toEqual(
            [automaticFrom, automaticTo],
          );
          await proof(page, `${width}-strict-mode-ready`);
        } finally {
          await close(test);
        }
      },
    );
  }
});
