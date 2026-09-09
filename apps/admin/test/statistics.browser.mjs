// Run: OPENDLE_UI_PATH=../opendle-ui node apps/admin/test/statistics.browser.mjs
/* global document, innerWidth, getComputedStyle, requestAnimationFrame, HTMLInputElement, Event, innerHeight */
// Real App and HTTP client; all traffic is handled by loopback-only fixtures.
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { URL } from "node:url";
import { after, before, it } from "node:test";

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
  process.env.LLMROUTER_STATISTICS_EVIDENCE ??
    "/tmp/llmrouter-statistics-browser",
);
const origin = "http://127.0.0.1:5174";
const instant = "2026-03-08T00:30:00.000Z";
const groups = [
  ["date", "Date"],
  ["call_actor", "Call actor"],
  ["service", "Service"],
  ["workspace", "Workspace"],
  ["administrator", "Administrator"],
  ["configuration_service", "Assignment configuration service"],
  ["assignment", "Assignment"],
  ["provider_model", "Provider route"],
  ["outcome", "Outcome"],
  ["tag", "Tag"],
];
const results = [];
let browser, script, css;
before(
  async () => {
    const bundle = await build({
      entryPoints: [
        resolve(root, "apps/admin/test/fixtures/statistics-browser.tsx"),
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
  },
  { timeout: 30_000 },
);
after(async () => {
  await browser?.close();
  await writeFile(
    resolve(evidence, "results.json"),
    JSON.stringify(results, null, 2) + "\n",
  );
});
const button = (page) =>
  page.getByRole("button", { name: "Run statistics", exact: true });
const advanced = (page) =>
  page.locator("summary").filter({ hasText: /^Advanced filters/ });
const groupSummary = (page) =>
  page.locator("summary").filter({ hasText: /^Group results/ });
const control = (page, label) =>
  page
    .getByLabel(label, { exact: true })
    .and(page.locator("input:not([type=checkbox]), select"))
    .filter({ visible: true });
async function open(width = 1440, timezoneId = "UTC") {
  const context = await browser.newContext({
    viewport: { width, height: width <= 390 ? 844 : 1000 },
    deviceScaleFactor: 1,
    timezoneId,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  await page.clock.setFixedTime(new Date(instant));
  const errors = [],
    requests = [],
    pending = [];
  const fixture = {
    context,
    page,
    errors,
    requests,
    pending,
    hold: false,
    fail: false,
    populated: false,
  };
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    expect(url.origin).toBe(origin);
    expect(route.request().method()).toBe("GET");
    if (url.pathname === "/statistics.js")
      return route.fulfill({ contentType: "text/javascript", body: script });
    if (url.pathname === "/statistics.css")
      return route.fulfill({ contentType: "text/css", body: css });
    if (url.pathname === "/v1/admin/session")
      return route.fulfill({
        json: {
          subject: "fixture-administrator",
          display_name: "Fixture administrator",
          expires_at: "2099-01-01T00:00:00Z",
          csrf_token: "synthetic-csrf",
        },
      });
    if (url.pathname === "/v1/admin/services")
      return route.fulfill({
        json: {
          items: [
            {
              api_name: "root",
              display_name: "Root service",
              is_root: true,
              parent_service_api_name: null,
              created_at: instant,
            },
          ],
          page: { has_more: false },
        },
      });
    if (url.pathname === "/v1/admin/statistics") {
      requests.push(url);
      if (fixture.hold) {
        pending.push({ route, url });
        return;
      }
      return respond({ route, url }, fixture.populated, fixture.fail);
    }
    expect(["/statistics", "/favicon.ico"]).toContain(url.pathname);
    return route.fulfill({
      contentType: "text/html",
      body: '<!doctype html><html lang="en"><head><title>Statistics fixture</title><link rel="stylesheet" href="/statistics.css"></head><body><div id="root"></div><script src="/statistics.js"></script></body></html>',
    });
  });
  await page.goto(origin + "/statistics");
  await expect(
    page.getByRole("heading", {
      name: "Usage and cost statistics",
      exact: true,
    }),
  ).toBeVisible();
  return fixture;
}
async function respond({ route, url }, populated = false, fail = false) {
  if (fail)
    return route.fulfill({
      status: 503,
      json: {
        error: { code: "unavailable", message: "Synthetic fixture failure." },
      },
    });
  const group_by = url.searchParams.getAll("group_by");
  const values = {
    date: "2026-03-08",
    call_actor: "administrator",
    service: "root",
    workspace: "workspace-fixture",
    administrator: "Synthetic administrator ".repeat(12),
    configuration_service: "root",
    assignment: "(exact)",
    provider_model: "route-fixture",
    outcome: "succeeded",
    tag: "synthetic-tag",
  };
  return route.fulfill({
    json: {
      from: url.searchParams.get("from"),
      to: url.searchParams.get("to"),
      group_by,
      buckets: populated
        ? [
            {
              dimensions: group_by.map((group) => values[group]),
              calls: 2,
              attempts: 3,
              units: [{ unit: "input_token", quantity: "42" }],
              cost: "0.0042",
              currency: "USD",
            },
          ]
        : [],
    },
  });
}
async function close(fixture) {
  await fixture.context.close();
  expect(fixture.errors).toEqual([]);
}
async function dates(page, from, through) {
  for (const [label, value] of [
    ["From", from],
    ["Through", through],
  ]) {
    const time = Date.parse(value + "T00:00:00Z");
    const valid =
      /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      !value.startsWith("0000") &&
      Number.isFinite(time) &&
      new Date(time).toISOString() === value + "T00:00:00.000Z";
    if (value && !valid) {
      // Native date controls reject invalid strings. Deliver them atomically
      // through the controlled input event to test Router validation as well.
      await control(page, label).evaluate((input, date) => {
        input.type = "text";
        Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype,
          "value",
        ).set.call(input, date);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }, value);
    } else {
      await control(page, label).fill(value);
    }
  }
}
async function state(page, message) {
  await expect(
    page.getByText(message, { exact: true }).filter({ visible: true }).first(),
  ).toBeVisible();
  await expect(
    page
      .locator('[aria-live], [role="status"], [role="alert"]')
      .filter({ hasText: message })
      .first(),
  ).toHaveCount(1);
}
// Read Chrome's native date shadow tree. Canvas width alone misses clipped years
// and the calendar button because they have separate native layout boxes.
async function nativeDateGeometry(page) {
  const cdp = await page.context().newCDPSession(page);
  const flatten = (node) => [
    node,
    ...[...(node.children ?? []), ...(node.shadowRoots ?? [])].flatMap(flatten),
  ];
  const records = [];
  try {
    const { root } = await cdp.send("DOM.getDocument", {
      depth: -1,
      pierce: true,
    });
    const nodes = flatten(root);
    for (const label of ["from", "through"]) {
      const input = nodes.find(
        (node) =>
          node.nodeName === "INPUT" &&
          node.attributes?.includes(`statistics-filter-${label}`),
      );
      expect(input).toBeTruthy();
      const parts = {};
      for (const node of flatten(input).filter((node) =>
        node.attributes?.includes("pseudo"),
      )) {
        const pseudo = node.attributes[node.attributes.indexOf("pseudo") + 1];
        const { object } = await cdp.send("DOM.resolveNode", {
          nodeId: node.nodeId,
        });
        const { result } = await cdp.send("Runtime.callFunctionOn", {
          objectId: object.objectId,
          returnByValue: true,
          functionDeclaration: `function () {
            const range = document.createRange();
            range.selectNodeContents(this);
            return { rect: this.getBoundingClientRect().toJSON(), text: range.getBoundingClientRect().toJSON(), content: this.textContent };
          }`,
        });
        parts[pseudo] = result.value;
      }
      const edit = parts["-webkit-datetime-edit"],
        picker = parts["-webkit-calendar-picker-indicator"];
      expect(edit).toBeTruthy();
      expect(picker.rect.width).toBeGreaterThan(0);
      const host = await page
        .locator(`#statistics-filter-${label}`)
        .boundingBox();
      expect(picker.rect.right).toBeLessThanOrEqual(host.x + host.width);
      expect(picker.rect.left).toBeGreaterThanOrEqual(edit.rect.right - 0.5);
      for (const name of ["month", "day", "year"]) {
        const field = parts[`-webkit-datetime-edit-${name}-field`];
        expect(field.text.width).toBeGreaterThan(0);
        expect(field.text.left).toBeGreaterThanOrEqual(edit.rect.left - 0.5);
        expect(field.text.right).toBeLessThanOrEqual(edit.rect.right + 0.5);
        expect(field.text.right).toBeLessThanOrEqual(picker.rect.left + 0.5);
        if (
          name === "year" &&
          (await page.locator(`#statistics-filter-${label}`).inputValue())
        )
          expect(field.content).toMatch(/^\d{4}$/);
      }
      records.push({ label, parts });
    }
  } finally {
    await cdp.detach();
  }
  return records;
}
async function focusGeometry(page) {
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  const geometry = await page.evaluate(() => {
    const active = document.activeElement;
    const row = active.closest(".od-radio-group-choice") ?? active;
    const rect = active.getBoundingClientRect(),
      bounds = row.getBoundingClientRect();
    const style = getComputedStyle(active);
    const outline =
      Number.parseFloat(style.outlineWidth) +
      Math.max(0, Number.parseFloat(style.outlineOffset));
    const nav = document.querySelector(".od-application-mobile-navigation");
    const bottom =
      nav && nav.getBoundingClientRect().height
        ? nav.getBoundingClientRect().top
        : innerHeight;
    return {
      id: active.id,
      label: row.textContent,
      top: Math.min(rect.top - outline, bounds.top),
      bottom: Math.max(rect.bottom + outline, bounds.bottom),
      left: Math.min(rect.left - outline, bounds.left),
      right: Math.max(rect.right + outline, bounds.right),
      limit: bottom,
      width: innerWidth,
    };
  });
  expect(geometry.top, JSON.stringify(geometry)).toBeGreaterThanOrEqual(-0.5);
  expect(geometry.bottom, JSON.stringify(geometry)).toBeLessThanOrEqual(
    geometry.limit + 0.5,
  );
  expect(geometry.left).toBeGreaterThanOrEqual(-0.5);
  expect(geometry.right).toBeLessThanOrEqual(geometry.width + 0.5);
  return geometry;
}
async function radioTextGeometry(page) {
  const rows = await page
    .locator(".od-radio-group-choice")
    .evaluateAll((items) =>
      items.map((row) => {
        const label = row.querySelector(".od-radio-group-choice-label");
        const range = document.createRange();
        range.selectNodeContents(label);
        const words = [];
        const text = label.firstChild;
        for (const match of text.textContent.matchAll(/\S+/g)) {
          range.setStart(text, match.index);
          range.setEnd(text, match.index + match[0].length);
          words.push({
            text: match[0],
            rects: [...range.getClientRects()].map((rect) => rect.toJSON()),
          });
        }
        return {
          text: label.textContent,
          row: row.getBoundingClientRect().toJSON(),
          label: label.getBoundingClientRect().toJSON(),
          words,
        };
      }),
    );
  expect(rows).toHaveLength(6);
  for (const row of rows) {
    for (const word of row.words) {
      // Complete words must fit. overflow-wrap:anywhere alone can hide poor
      // composition by splitting Administrator across narrow lines.
      expect(word.rects).toHaveLength(1);
      expect(word.rects[0].left).toBeGreaterThanOrEqual(row.label.left - 0.5);
      expect(word.rects[0].right).toBeLessThanOrEqual(row.label.right + 0.5);
      expect(word.rects[0].bottom).toBeLessThanOrEqual(row.row.bottom + 0.5);
    }
  }
  return rows;
}

async function proof(page, name) {
  const nativeDates = await nativeDateGeometry(page);
  const layout = await page
    .getByRole("form", { name: "Usage and cost filters", exact: true })
    .evaluate((form) => {
      const rect = (element) => element.getBoundingClientRect().toJSON();
      return {
        form: rect(form),
        advanced: rect(form.querySelector("#statistics-advanced")),
        table: rect(document.querySelector(".administration-data-table")),
        heading: rect(document.querySelector(".od-page-heading")),
      };
    });
  for (const item of [layout.advanced, layout.table, layout.heading]) {
    expect(item.left).toBeCloseTo(layout.form.left, 0);
    expect(item.right).toBeCloseTo(layout.form.right, 0);
  }
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const axe = await new AxeBuilder({ page }).analyze();
  expect(axe.violations).toEqual([]);
  await page.screenshot({
    path: resolve(evidence, name + ".png"),
    fullPage: true,
  });
  const cards = await page
    .locator(".od-data-table-card")
    .filter({ visible: true })
    .evaluateAll((items) =>
      items.map((card) => {
        const value = card.querySelector("dd");
        return {
          heading: card.querySelector(".od-data-table-card-title").textContent,
          width: card.getBoundingClientRect().width,
          dimensionWidth: value.getBoundingClientRect().width,
          dimensionHeight: value.getBoundingClientRect().height,
          fontSize: getComputedStyle(value).fontSize,
        };
      }),
    );
  for (const [index, card] of cards.entries()) {
    expect(card.heading).toBe(`Statistics group ${index + 1}`);
  }
  results.push({
    name,
    axeViolations: axe.violations,
    overflow: false,
    cards,
    layout,
    nativeDates,
  });
}
async function invalid(fixture, from, through, label, message) {
  await dates(fixture.page, from, through);
  const count = fixture.requests.length;
  await button(fixture.page).click();
  await expect(control(fixture.page, label)).toBeFocused();
  await expect(control(fixture.page, label)).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  const error = await control(fixture.page, label).getAttribute(
    "aria-describedby",
  );
  expect(error).toBeTruthy();
  expect(
    await fixture.page
      .locator(
        error
          .split(/\s+/)
          .map((id) => `[id="${id}"]`)
          .join(","),
      )
      .allTextContents(),
  ).toContain(message);
  await state(fixture.page, message);
  expect(fixture.requests.length).toBe(count);
}

for (const zone of [
  "UTC",
  "Pacific/Kiritimati",
  "Pacific/Pago_Pago",
  "America/New_York",
]) {
  it(`uses identical fixed-instant UTC dates and boundaries in ${zone}`, async () => {
    const fixture = await open(1440, zone),
      { page } = fixture;
    try {
      await expect(control(page, "From")).toHaveValue("2026-02-07");
      await expect(control(page, "Through")).toHaveValue("2026-03-08");
      await expect(control(page, "From")).toHaveAttribute("type", "date");
      await expect(control(page, "Through")).toHaveAttribute("type", "date");
      const cases = [
        ["2026-02-07", "2026-03-08", "2026-03-09"],
        ["2026-03-08", "2026-03-08", "2026-03-09"],
        ["2026-08-29", "2026-08-29", "2026-08-30"],
        ["2026-10-31", "2026-11-02", "2026-11-03"],
        ["2026-04-01", "2026-04-30", "2026-05-01"],
        ["2026-12-31", "2026-12-31", "2027-01-01"],
        ["2028-02-29", "2028-02-29", "2028-03-01"],
        ["2000-02-29", "2000-02-29", "2000-03-01"],
        ["2028-01-01", "2028-12-31", "2029-01-01"],
        ["0099-12-31", "0099-12-31", "0100-01-01"],
      ];
      for (const [from, through, next] of cases) {
        await dates(page, from, through);
        await button(page).click();
        await state(page, "No usage or cost matches these filters.");
        expect([...fixture.requests.at(-1).searchParams.entries()]).toEqual([
          ["from", from + "T00:00:00Z"],
          ["to", next + "T00:00:00Z"],
        ]);
      }
      await invalid(
        fixture,
        "2028-01-01",
        "2029-01-01",
        "Through",
        "Select 366 dates or fewer.",
      );
      await invalid(
        fixture,
        "2026-03-09",
        "2026-03-08",
        "Through",
        "Through must be the same as or after From.",
      );
      await invalid(
        fixture,
        "9999-12-31",
        "9999-12-31",
        "Through",
        "Through is outside the supported date range.",
      );
      await invalid(
        fixture,
        "",
        "2026-03-08",
        "From",
        "Enter a valid From date.",
      );
      await invalid(
        fixture,
        "2026-03-08",
        "",
        "Through",
        "Enter a valid Through date.",
      );
      // Native date inputs reject these strings before React receives them.
      // Text input exposes the same controlled change path for invalid syntax.
      for (const from of [
        "2026-02-29",
        "2026-04-31",
        "1900-02-29",
        "0000-01-01",
        "2026-2-1",
      ]) {
        await invalid(
          fixture,
          from,
          "2026-03-08",
          "From",
          "Enter a valid From date.",
        );
      }
      await control(page, "From").evaluate((input) => {
        input.type = "date";
      });
      results.push({
        name: zone,
        instant,
        exactDateCases: cases.length,
        invalidCases: 10,
      });
    } finally {
      await close(fixture);
    }
  });
}

for (const width of [1440, 390, 320]) {
  it(`proves basic, advanced, validation, query states and groups at ${width}px`, async () => {
    const fixture = await open(width),
      { page } = fixture;
    try {
      for (const label of ["From", "Through", "Service", "Workspace"])
        await expect(control(page, label)).toBeVisible();
      await expect(advanced(page)).toHaveText("Advanced filters");
      await expect(advanced(page).locator("..")).not.toHaveAttribute(
        "open",
        "",
      );
      expect(
        await button(page).evaluate(
          (element) => element.closest("details") === null,
        ),
      ).toBe(true);
      await expect(
        page.getByText(
          "UTC dates. From and Through include the selected dates.",
          { exact: true },
        ),
      ).toBeVisible();
      await proof(page, `statistics-basic-${width}`);
      await advanced(page).click();
      for (const label of [
        "Administrator",
        "Assignment configuration service",
        "Assignment",
        "Provider route",
        "Tag",
      ])
        await expect(control(page, label)).toBeVisible();
      await expect(
        page.getByRole("radio", { name: "All call actors", exact: true }),
      ).toHaveCount(1);
      await expect(
        page.getByRole("option", { name: "All services", exact: true }),
      ).toHaveCount(1);
      await expect(
        page.getByRole("radio", { name: "All outcomes", exact: true }),
      ).toHaveCount(1);
      for (const label of [
        "Service calls",
        "Administrator playground calls",
        "Succeeded",
        "Failed",
      ])
        await expect(
          page.getByRole("radio", { name: label, exact: true }),
        ).toHaveCount(1);
      await expect(groupSummary(page)).toHaveText("Group results (0 selected)");
      await expect(page.getByRole("checkbox")).toHaveCount(0);
      await groupSummary(page).focus();
      await page.keyboard.press("Enter");
      for (const [, label] of groups)
        await expect(
          page.getByRole("checkbox", { name: label, exact: true }),
        ).toBeVisible();
      await page.keyboard.press("Tab");
      await expect(
        page.getByRole("checkbox", { name: "Date", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Space");
      await page.keyboard.press("Tab");
      await expect(
        page.getByRole("checkbox", { name: "Call actor", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Shift+Tab");
      await expect(
        page.getByRole("checkbox", { name: "Date", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(groupSummary(page)).toBeFocused();
      await expect(page.getByRole("checkbox")).toHaveCount(0);
      await page.keyboard.press("Space");
      for (const [, label] of groups.slice(1, 8).reverse())
        await page.getByRole("checkbox", { name: label, exact: true }).check();
      await expect(groupSummary(page)).toHaveText("Group results (8 selected)");
      await state(page, "Select up to 8 groups.");
      for (const [, label] of groups.slice(0, 8))
        await expect(
          page.getByRole("checkbox", { name: label, exact: true }),
        ).toBeEnabled();
      for (const [, label] of groups.slice(8))
        await expect(
          page.getByRole("checkbox", { name: label, exact: true }),
        ).toBeDisabled();
      await expect(advanced(page)).toHaveText("Advanced filters (8 active)");
      const scroll = await page
        .locator(".od-compact-checkbox-group-options")
        .evaluate((element) => ({
          height: element.clientHeight,
          full: element.scrollHeight,
          overflow: getComputedStyle(element).overflowY,
        }));
      expect(["auto", "scroll"]).toContain(scroll.overflow);
      expect(scroll.full).toBeGreaterThan(scroll.height);
      await proof(page, `statistics-advanced-${width}`);
      await advanced(page).click();
      await expect(advanced(page)).toHaveText("Advanced filters (8 active)");
      await advanced(page).click();
      await expect(
        page.getByRole("checkbox", { name: "Date", exact: true }),
      ).toBeChecked();
      await invalid(fixture, "", "", "From", "Enter a valid From date.");
      await expect(control(page, "Through")).toHaveAttribute(
        "aria-invalid",
        "true",
      );
      await proof(page, `statistics-error-${width}`);
      await dates(page, "2026-03-08", "2026-03-08");
      await expect(control(page, "From")).not.toHaveAttribute(
        "aria-invalid",
        "true",
      );
      await expect(
        page.getByText("Enter a valid From date.", { exact: true }),
      ).toHaveCount(0);
      await control(page, "Tag").fill("x".repeat(129));
      await advanced(page).click();
      await button(page).click();
      await expect(control(page, "Tag")).toBeFocused();
      await expect(advanced(page).locator("..")).toHaveAttribute("open", "");
      await state(page, "Enter a tag of 128 UTF-8 bytes or fewer.");
      await control(page, "Tag").fill("");
      await expect(
        page.getByText("Enter a tag of 128 UTF-8 bytes or fewer.", {
          exact: true,
        }),
      ).toHaveCount(0);
      fixture.hold = true;
      await button(page).click();
      await expect(button(page)).toBeFocused();
      await state(page, "Loading usage and cost.");
      await expect(page.locator('[aria-busy="true"]').first()).toBeVisible();
      await expect.poll(() => fixture.pending.length).toBe(1);
      await button(page).dispatchEvent("click");
      expect(fixture.requests.length).toBe(1);
      await respond(fixture.pending.shift());
      await state(page, "No usage or cost matches these filters.");
      await expect(button(page)).toBeFocused();
      await proof(page, `statistics-empty-${width}`);
      await button(page).click();
      await expect.poll(() => fixture.pending.length).toBe(1);
      await respond(fixture.pending.shift(), false, true);
      await state(
        page,
        "Unable to load usage and cost. Review the filters and try again.",
      );
      await expect(button(page)).toBeFocused();
      await proof(page, `statistics-api-error-${width}`);
      await button(page).click();
      await expect.poll(() => fixture.pending.length).toBe(1);
      expect(fixture.requests.at(-1).searchParams.getAll("group_by")).toEqual(
        groups.slice(0, 8).map(([value]) => value),
      );
      await respond(fixture.pending.shift(), true);
      await expect(
        page
          .getByText("USD 0.0042", { exact: true })
          .filter({ visible: true })
          .first(),
      ).toBeVisible();
      await expect(button(page)).toBeFocused();
      expect(
        await page
          .locator('[aria-live], [role="status"]')
          .filter({ hasText: /loaded|ready/i })
          .count(),
      ).toBeGreaterThan(0);
      const resultDimensions = page
        .getByText(/^Date: 2026-03-08/)
        .filter({ visible: true })
        .first();
      const dimensions = await resultDimensions.innerText();
      expect(
        dimensions.split(" / ").map((value) => value.split(": ")[0]),
      ).toEqual(groups.slice(0, 8).map(([, label]) => label));
      await proof(page, `statistics-populated-${width}`);
      await page.getByRole("checkbox", { name: "Date", exact: true }).uncheck();
      await expect(resultDimensions).toHaveText(dimensions);
      await page.evaluate(() => {
        document.documentElement.style.fontSize = "200%";
      });
      await proof(page, `statistics-text-200-${width}`);
      if (width === 390) {
        await page
          .locator(".od-data-table-card")
          .first()
          .evaluate((card) => {
            card.scrollIntoView({ block: "start" });
          });
        await page.screenshot({
          path: resolve(
            evidence,
            "statistics-text-200-390-result-viewport.png",
          ),
        });
      }
      await button(page).scrollIntoViewIfNeeded();
      await expect(button(page)).toBeVisible();
    } finally {
      await close(fixture);
    }
  });
}

it("sends all active filters and displayed group order, then omits cleared values", async () => {
  const fixture = await open(),
    { page } = fixture;
  try {
    await advanced(page).click();
    await control(page, "Service").selectOption("root");
    await control(page, "Workspace").fill("workspace-fixture");
    await page
      .getByRole("radio", {
        name: "Administrator playground calls",
        exact: true,
      })
      .check();
    await control(page, "Administrator").fill("fixture-administrator");
    await control(page, "Assignment configuration service").fill("root");
    await control(page, "Assignment").fill("Exact provider route calls");
    await control(page, "Provider route").fill("route-fixture");
    await page.getByRole("radio", { name: "Succeeded", exact: true }).check();
    await control(page, "Tag").fill("synthetic-tag");
    await groupSummary(page).click();
    await page.getByRole("checkbox", { name: "Tag", exact: true }).check();
    await page
      .getByRole("checkbox", { name: "Call actor", exact: true })
      .check();
    await expect(advanced(page)).toHaveText("Advanced filters (9 active)");
    await button(page).click();
    await state(page, "No usage or cost matches these filters.");
    expect(
      [...fixture.requests.at(-1).searchParams.entries()].sort(
        ([left], [right]) => left.localeCompare(right),
      ),
    ).toEqual(
      [
        ["from", "2026-02-07T00:00:00Z"],
        ["to", "2026-03-09T00:00:00Z"],
        ["service", "root"],
        ["workspace", "workspace-fixture"],
        ["call_actor", "administrator"],
        ["administrator", "fixture-administrator"],
        ["configuration_service", "root"],
        ["assignment", "(exact)"],
        ["provider_model", "route-fixture"],
        ["outcome", "succeeded"],
        ["tag", "synthetic-tag"],
        ["group_by", "call_actor"],
        ["group_by", "tag"],
      ].sort(([left], [right]) => left.localeCompare(right)),
    );
    await control(page, "Service").selectOption("");
    await page
      .getByRole("radio", { name: "All call actors", exact: true })
      .check();
    await page
      .getByRole("radio", { name: "All outcomes", exact: true })
      .check();
    for (const label of [
      "Workspace",
      "Administrator",
      "Assignment configuration service",
      "Assignment",
      "Provider route",
      "Tag",
    ])
      await control(page, label).fill("");
    await page
      .getByRole("checkbox", { name: "Call actor", exact: true })
      .uncheck();
    await page.getByRole("checkbox", { name: "Tag", exact: true }).uncheck();
    await expect(advanced(page)).toHaveText("Advanced filters");
    await button(page).click();
    await state(page, "No usage or cost matches these filters.");
    expect([...fixture.requests.at(-1).searchParams.keys()]).toEqual([
      "from",
      "to",
    ]);
  } finally {
    await close(fixture);
  }
});

it("keeps the later query and validation focus when older requests finish", async () => {
  const fixture = await open(),
    { page } = fixture;
  try {
    fixture.hold = true;
    for (const fail of [false, true]) {
      await dates(page, "2026-03-08", "2026-03-08");
      await button(page).click();
      await expect.poll(() => fixture.pending.length).toBe(1);
      await dates(page, "2026-03-09", "2026-03-09");
      await button(page).click();
      await expect.poll(() => fixture.pending.length).toBe(2);
      await expect(button(page)).toBeFocused();
      await respond(fixture.pending.pop(), true);
      await expect(
        page
          .getByText("USD 0.0042", { exact: true })
          .filter({ visible: true })
          .first(),
      ).toBeVisible();
      await control(page, "Workspace").focus();
      const before = await page.locator("body").innerText();
      await respond(fixture.pending.pop(), !fail, fail);
      await page.evaluate(
        () =>
          new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          ),
      );
      await expect(control(page, "Workspace")).toBeFocused();
      expect(await page.locator("body").innerText()).toBe(before);
    }
    await button(page).click();
    await expect.poll(() => fixture.pending.length).toBe(1);
    await invalid(
      fixture,
      "",
      "2026-03-09",
      "From",
      "Enter a valid From date.",
    );
    const before = await page.locator("body").innerText();
    await respond(fixture.pending.pop(), false, true);
    await page.evaluate(
      () =>
        new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)),
        ),
    );
    await expect(control(page, "From")).toBeFocused();
    expect(await page.locator("body").innerText()).toBe(before);
  } finally {
    await close(fixture);
  }
});

for (const width of [1440, 390, 320]) {
  for (const scale of [100, 200]) {
    it(`keeps native dates, radio words, focus and results usable at ${width}px and ${scale}% text`, async () => {
      const fixture = await open(width),
        { page } = fixture;
      try {
        await page.evaluate((size) => {
          document.documentElement.style.fontSize = `${size}%`;
        }, scale);
        await expect(control(page, "Service")).toHaveValue("");
        const service = await control(page, "Service").evaluate((select) => {
          const style = getComputedStyle(select),
            canvas = document.createElement("canvas");
          const context = canvas.getContext("2d");
          context.font = style.font;
          return {
            text: select.selectedOptions[0].textContent,
            measured: context.measureText(select.selectedOptions[0].textContent)
              .width,
            available:
              select.clientWidth -
              Number.parseFloat(style.paddingLeft) -
              Number.parseFloat(style.paddingRight),
          };
        });
        expect(service.text).toBe("All services");
        expect(service.available).toBeGreaterThanOrEqual(service.measured);
        await proof(page, `statistics-native-basic-${width}-${scale}`);
        await advanced(page).click();
        const radioRows = await radioTextGeometry(page);
        await expect(
          page.getByRole("radio", { name: "All call actors", exact: true }),
        ).toBeChecked();
        await expect(
          page.getByRole("radio", { name: "All outcomes", exact: true }),
        ).toBeChecked();
        const focus = [];
        await control(page, "From").focus();
        let reachedRun = false;
        for (let index = 0; index < 45; index++) {
          focus.push(await focusGeometry(page));
          if (
            await button(page).evaluate(
              (element) => element === document.activeElement,
            )
          ) {
            reachedRun = true;
            break;
          }
          if (
            await page
              .getByRole("radio", { name: "All call actors", exact: true })
              .evaluate((element) => element === document.activeElement)
          ) {
            for (const label of [
              "Service calls",
              "Administrator playground calls",
              "All call actors",
            ]) {
              await page.keyboard.press("ArrowDown");
              await expect(
                page.getByRole("radio", { name: label, exact: true }),
              ).toBeFocused();
              await expect(
                page.getByRole("radio", { name: label, exact: true }),
              ).toBeChecked();
              focus.push(await focusGeometry(page));
              await page.screenshot({
                path: resolve(
                  evidence,
                  `statistics-radio-${width}-${scale}-${label.split(" ")[0]}.png`,
                ),
              });
            }
          }
          if (
            await page
              .getByRole("radio", { name: "All outcomes", exact: true })
              .evaluate((element) => element === document.activeElement)
          ) {
            for (const label of ["Succeeded", "Failed", "All outcomes"]) {
              await page.keyboard.press("ArrowDown");
              await expect(
                page.getByRole("radio", { name: label, exact: true }),
              ).toBeFocused();
              await expect(
                page.getByRole("radio", { name: label, exact: true }),
              ).toBeChecked();
              focus.push(await focusGeometry(page));
            }
          }
          await page.keyboard.press("Tab");
        }
        expect(reachedRun).toBe(true);
        let reachedFrom = false;
        for (let index = 0; index < 45; index++) {
          await page.keyboard.press("Shift+Tab");
          focus.push(await focusGeometry(page));
          if (
            await control(page, "From").evaluate(
              (element) => element === document.activeElement,
            )
          ) {
            reachedFrom = true;
            break;
          }
        }
        expect(reachedFrom).toBe(true);
        await proof(page, `statistics-native-advanced-${width}-${scale}`);
        fixture.populated = true;
        await button(page).click();
        await expect(
          page
            .getByText("USD 0.0042", { exact: true })
            .filter({ visible: true })
            .first(),
        ).toBeVisible();
        const cards = await page
          .locator(".od-data-table-card-value")
          .filter({ visible: true })
          .evaluateAll((items) =>
            items.map((row) => {
              const style = getComputedStyle(row),
                label = row.querySelector("dt"),
                value = row.querySelector("dd");
              return {
                width:
                  row.clientWidth -
                  Number.parseFloat(style.paddingLeft) -
                  Number.parseFloat(style.paddingRight),
                label: label.getBoundingClientRect().toJSON(),
                value: value.getBoundingClientRect().toJSON(),
                text: value.textContent,
                stacked:
                  row.closest(".od-data-table").clientWidth <=
                  22 *
                    Number.parseFloat(
                      getComputedStyle(document.documentElement).fontSize,
                    ),
                scrollWidth: value.scrollWidth,
                clientWidth: value.clientWidth,
              };
            }),
          );
        if (width <= 390) {
          expect(cards.length).toBeGreaterThan(0);
          for (const card of cards) {
            if (card.stacked) {
              expect(card.label.width).toBeCloseTo(card.width, 0);
              expect(card.value.width).toBeCloseTo(card.width, 0);
              expect(card.value.top).toBeGreaterThanOrEqual(card.label.bottom);
            } else {
              expect(card.value.left).toBeGreaterThan(card.label.right);
              expect(card.value.width).toBeGreaterThan(card.width / 2);
            }
            expect(card.scrollWidth).toBeLessThanOrEqual(card.clientWidth + 1);
          }
          await page
            .locator(".od-data-table-card")
            .first()
            .evaluate((card) => card.scrollIntoView({ block: "start" }));
          await page.screenshot({
            path: resolve(
              evidence,
              `statistics-native-result-${width}-${scale}-viewport.png`,
            ),
          });
        }
        await proof(page, `statistics-native-result-${width}-${scale}`);
        results.push({
          name: `native-${width}-${scale}`,
          service,
          radioRows,
          focus,
          cards,
        });
      } finally {
        await close(fixture);
      }
    });
  }
}
