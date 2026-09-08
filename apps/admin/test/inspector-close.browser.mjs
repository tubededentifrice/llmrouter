// Run with: node apps/admin/test/inspector-close.browser.mjs
// Uses the installed OpenDLE UI browser tools. All browser requests are controlled.
/* global console, document, window */
import { strict as assert } from "node:assert";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { argv } from "node:process";
import { fileURLToPath, URL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const shared = resolve(root, "../opendle-ui");
const requireShared = createRequire(resolve(shared, "package.json"));
const { chromium, expect } = requireShared("@playwright/test");
const { build } = requireShared("esbuild");
const source = String.raw`
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {ConfigurationGraph} from './apps/admin/src/ConfigurationGraph.tsx';
import {ServiceManagement} from './apps/admin/src/ServiceManagement.tsx';
const date = '2026-08-25T00:00:00Z';
const service = {api_name:'fixture', display_name:'Fixture service', created_at:date};
const provider = {api_name:'fixture-provider',display_name:'Fixture provider',adapter:'openrouter',credential_api_name:'fixture-credential',enabled:true,created_at:date};
const model = {api_name:'fixture-model',display_name:'Fixture model',input_modalities:['text'],output_modalities:['text'],capabilities:['streaming'],constraints:{},price_source:'manual',created_at:date};
const mapping = {api_name:'fixture-route',provider_api_name:provider.api_name,model_api_name:model.api_name,provider_model_name:'fixture/model',enabled:true,input_modalities:['text'],output_modalities:['text'],capabilities:['streaming'],reasoning_mappings:[],created_at:date};
const assignment = {api_name:'default',display_name:'Fixture assignment',definition_kind:'direct_chain',defined_by_service_api_name:service.api_name,direct_chain:[{provider_model_api_name:mapping.api_name}],effective_chain:[{provider_model_api_name:mapping.api_name}],observed_requirements:[]};
const pending = new Map();
const calls = [];
const workspaceRecords = [];
const keyRecords = new Map();
function defer(name, args, value) {
  calls.push({name,args});
  if(pending.has(name)) throw Error('Duplicate request: '+name);
  return new Promise((resolve,reject) => pending.set(name,{resolve,reject,value}));
}
const client = {
  putProvider:(...args)=>defer('provider',args,{...provider,...args[1]}),
  putModel:(...args)=>defer('model',args,{...model,...args[1]}),
  putProviderModel:(...args)=>defer('mapping',args,{...mapping,...args[1]}),
  putAssignment:(...args)=>defer('assignment',args,{...assignment,...args[2]}),
  updateService:(...args)=>defer('service',args,{...service,...args[1]}),
  createService:(...args)=>defer('create',args,{...args[0],created_at:date}),
  createWorkspace:(...args)=>defer('workspace',args,{...args[1],service_api_name:service.api_name,created_at:date}),
  createKey:(...args)=>defer('key',args,{key:{id:'fixture-key',name:args[1],created_at:date},secret:'synthetic-close-test-key'}),
  keys:async(name)=>({items:keyRecords.get(name) ?? []}),
  workspaces:async(name)=>({items:workspaceRecords.filter(item=>item.service_api_name===name)}),
};
window.fixture = {
  calls, pending,
  readWorkspaces:()=>client.workspaces(service.api_name),
  finish(name, fail=false) {
    const operation=pending.get(name);
    if(!operation) throw Error('No pending request: '+name);
    pending.delete(name);
    if(fail) operation.reject(Error('Controlled request failure'));
    else {
      if(name === "workspace") workspaceRecords.push(operation.value);
      if(name === "key") keyRecords.set(service.api_name,[...(keyRecords.get(service.api_name) ?? []),operation.value.key]);
      operation.resolve(operation.value);
    }
  }
};
function Fixture() {
  const [selected,setSelected]=useState('');
  const [services,setServices]=useState([service]);
  const [notice,setNotice]=useState('');
  window.fixture.removeService=(empty=false)=>setServices(empty?[]:[{api_name:'remaining',display_name:'Remaining service',created_at:date}]);
  const props={client,csrf:'synthetic-csrf',onNotice:(_tone,message)=>setNotice(message)};
  return <main><h1>Inspector close test</h1><p role="status">{notice}</p>{window.fixtureKind==='configuration'
    ? <ConfigurationGraph {...props} assignments={[assignment]} credentials={[{api_name:'fixture-credential',fingerprint:'sha256:fixture',created_at:date,updated_at:date}]} models={[model]} providers={[provider]} providerModels={[mapping]} selectedService={service.api_name} services={services} onAssignmentDirtyChange={()=>{}} onRefreshAssignments={async()=>{}} onRefreshGlobal={async()=>{}}/>
    : <ServiceManagement {...props} services={services} selectedService={selected} onSelect={setSelected} onRefresh={async()=>{}}/>}</main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
`;
const bundle = await build({
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
  stdin: { contents: source, loader: "jsx", resolveDir: root },
});
const css = await readFile(resolve(shared, "styles/tokens.css"), "utf8");
const hostCss = await readFile(
  resolve(root, "apps/admin/src/styles.css"),
  "utf8",
);
const browser = await chromium.launch({
  executablePath: existsSync("/usr/bin/google-chrome")
    ? "/usr/bin/google-chrome"
    : undefined,
  headless: true,
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
});
const page = await context.newPage();
page.setDefaultTimeout(5000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
let kind = "configuration";
await context.route("**/*", async (route) => {
  assert.equal(new URL(route.request().url()).origin, "http://127.0.0.1:5174");
  if (route.request().url().endsWith("/fixture.js")) {
    await route.fulfill({
      contentType: "text/javascript",
      body: bundle.outputFiles[0].text,
    });
  } else if (route.request().url().endsWith("/fixture.css")) {
    await route.fulfill({ contentType: "text/css", body: css + hostCss });
  } else {
    await route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html lang="en"><head><title>Inspector close test</title><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script>window.fixtureKind=${JSON.stringify(kind)}</script><script src="/fixture.js"></script></body></html>`,
    });
  }
});
async function load(nextKind) {
  kind = nextKind;
  await page.goto("http://127.0.0.1:5174/inspector-close-fixture");
  await page.getByRole("heading", { name: "Inspector close test" }).waitFor();
}
async function requestClose(inspector, method) {
  if (method === "button")
    await inspector
      .getByRole("button", { name: /^Close (inspector|create service)$/ })
      .click();
  else {
    await inspector.getByRole("heading").first().focus();
    await page.keyboard.press("Escape");
  }
}
async function exactFocus(control) {
  await expect(control).toBeFocused();
}
const failures = [];
async function blocked(inspector, label, method, notice) {
  const close = inspector.getByRole("button", {
    name: /^Close (inspector|create service)$/,
  });
  await expect(close).toBeVisible();
  await expect(close).toBeEnabled();
  if (method === "button") await close.click();
  else {
    await inspector.getByRole("heading").first().focus();
    await page.keyboard.press("Escape");
  }
  await expect(inspector).toBeVisible();
  if (notice)
    await expect(page.locator("main > p[role=status]")).toHaveText(notice);
  const state = await inspector.evaluate((element) => ({
    mode: element.dataset.mode,
    open: element.open,
    modal: element.matches(":modal"),
    focusInside: element.contains(document.activeElement),
  }));
  try {
    assert.equal(state.open, true, "A denied close must keep the dialog open");
    assert.equal(
      state.modal,
      state.mode === "sheet",
      "A denied close must keep the modal state",
    );
    assert.equal(
      state.focusInside,
      true,
      "A denied close must keep focus in the inspector",
    );
  } catch (error) {
    failures.push({ label, method, ...state, error: error.message });
  }
}
async function pending(name) {
  await page.waitForFunction((key) => window.fixture.pending.has(key), name);
}
async function finish(name, fail = false) {
  await page.evaluate(({ name, fail }) => window.fixture.finish(name, fail), {
    name,
    fail,
  });
}
async function closeNormally(inspector, opener, method = "button") {
  if (method === "Escape") {
    await inspector.getByRole("heading").first().focus();
    await page.keyboard.press("Escape");
  } else
    await inspector
      .getByRole("button", { name: /^Close (inspector|create service)$/ })
      .click();
  await expect(inspector).toHaveCount(0);
  await exactFocus(opener);
}
async function openService() {
  const opener = page.locator('[data-service-api-name="fixture"]');
  await opener.click();
  const inspector = page.getByRole("dialog", {
    name: "Fixture service",
    exact: true,
  });
  await expect(inspector).toBeVisible();
  return { opener, inspector };
}
try {
  if (!argv.includes("--remaining")) {
    for (const [mode, width] of [
      ["split", 1440],
      ["overlay", 1000],
      ["sheet", 390],
    ]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const method of ["button", "Escape"]) {
        await load("configuration");
        const providerOpener = page.locator(
          '[data-node-id="provider:fixture-provider"]',
        );
        await providerOpener.click();
        const providerInspector = page.getByRole("dialog", {
          name: "Fixture provider",
          exact: true,
        });
        await expect(providerInspector).toHaveAttribute("data-mode", mode);
        await providerInspector
          .getByRole("button", { name: "Review and save provider" })
          .click();
        await pending("provider");
        await blocked(providerInspector, "provider pending", method);
        assert.equal(
          await page.evaluate(() => window.fixture.pending.has("provider")),
          true,
        );
        await finish("provider");
        await expect(
          providerInspector.getByRole("button", {
            name: "Review and save provider",
          }),
        ).toBeEnabled();
        await closeNormally(providerInspector, providerOpener, method);

        await load("service");
        const { opener, inspector } = await openService();
        await expect(inspector).toHaveAttribute("data-mode", mode);
        await inspector
          .getByRole("button", { name: "Save service", exact: true })
          .click();
        await pending("service");
        await blocked(
          inspector,
          "service pending",
          method,
          "Wait for the service request to finish before you close this inspector.",
        );
        await finish("service");
        await expect(
          inspector.getByRole("button", { name: "Save service", exact: true }),
        ).toBeEnabled();
        await closeNormally(inspector, opener, method);

        await load("service");
        const createOpener = page.getByRole("button", {
          name: "Create service",
          exact: true,
        });
        await createOpener.click();
        const createInspector = page.getByRole("dialog", {
          name: "Create service",
          exact: true,
        });
        await expect(createInspector).toHaveAttribute("data-mode", mode);
        await createInspector
          .getByRole("textbox", { name: "API name" })
          .fill("new-fixture");
        await createInspector
          .getByRole("textbox", { name: "Display name" })
          .fill("New fixture");
        await createInspector
          .getByRole("button", { name: "Create service", exact: true })
          .click();
        await pending("create");
        await blocked(
          createInspector,
          "service create pending",
          method,
          "Wait for the service request to finish before you close this inspector.",
        );
        await finish("create", true);
        await expect(
          createInspector.getByText("The service was not created.", {
            exact: true,
          }),
        ).toBeVisible();
        await expect(
          createInspector.getByRole("textbox", { name: "Display name" }),
        ).toHaveValue("New fixture");
        await closeNormally(createInspector, createOpener, method);
      }
    }
  }
  for (const [mode, width] of [
    ["split", 1440],
    ["overlay", 1000],
    ["sheet", 390],
  ]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const method of ["button", "Escape"]) {
      for (const [name, nodeId, title, saveLabel] of [
        [
          "model",
          "model:fixture-model",
          "Fixture model",
          "Save canonical model",
        ],
        [
          "mapping",
          "mapping:fixture-route",
          "Fixture provider",
          "Save provider route",
        ],
        [
          "assignment",
          "assignment:default",
          "Fixture assignment",
          "Save selected service assignment",
        ],
      ]) {
        await load("configuration");
        const opener = page.locator(`[data-node-id="${nodeId}"]`);
        await opener.click();
        const inspector = page.getByRole("dialog", {
          name: title,
          exact: true,
        });
        await expect(inspector).toHaveAttribute("data-mode", mode);
        const save = inspector.getByRole("button", {
          name: saveLabel,
          exact: true,
        });
        await save.click();
        await pending(name);
        await blocked(inspector, `${name} pending`, method);
        assert.equal(
          await page.evaluate((key) => window.fixture.pending.has(key), name),
          true,
        );
        assert.equal(
          await page.evaluate(
            (key) =>
              window.fixture.calls.filter((call) => call.name === key).length,
            name,
          ),
          1,
        );
        await finish(name);
        await expect(save).toBeEnabled();
        await closeNormally(inspector, opener, method);
      }

      await load("configuration");
      const assignmentOpener = page.locator(
        '[data-node-id="assignment:default"]',
      );
      await assignmentOpener.click();
      const assignmentInspector = page.getByRole("dialog", {
        name: "Fixture assignment",
        exact: true,
      });
      const displayName = assignmentInspector.getByRole("textbox", {
        name: "Display name",
        exact: true,
      });
      await displayName.fill("Keep this assignment draft");
      await requestClose(assignmentInspector, method);
      const confirmation = page.getByRole("dialog", {
        name: "Discard assignment changes?",
        exact: true,
      });
      await expect(confirmation).toBeVisible();
      await expect(
        confirmation.getByRole("button", { name: "Cancel", exact: true }),
      ).toBeFocused();
      await confirmation
        .getByRole("button", { name: "Cancel", exact: true })
        .click();
      await expect(confirmation).toHaveCount(0);
      await expect(displayName).toHaveValue("Keep this assignment draft");
      await expect(assignmentInspector).toHaveJSProperty("open", true);
      assert.equal(
        await assignmentInspector.evaluate((element) =>
          element.contains(document.activeElement),
        ),
        true,
      );
      await requestClose(assignmentInspector, method);
      await page.keyboard.press("Escape");
      await expect(confirmation).toHaveCount(0);
      await expect(displayName).toHaveValue("Keep this assignment draft");
      await requestClose(assignmentInspector, method);
      await confirmation
        .getByRole("textbox", {
          name: "Enter the impact statement to continue",
          exact: true,
        })
        .fill("discard unsaved assignment changes for service fixture");
      await confirmation
        .getByRole("button", { name: "Discard changes", exact: true })
        .click();
      await expect(assignmentInspector).toHaveCount(0);
      await exactFocus(assignmentOpener);
      assert.equal(await page.evaluate(() => window.fixture.calls.length), 0);

      await load("service");
      const workspaceService = await openService();
      await workspaceService.inspector
        .getByRole("button", { name: "Create workspace", exact: true })
        .click();
      const workspaceTable = workspaceService.inspector.locator(
        '[aria-label="Workspaces for Fixture service editor"]',
      );
      await workspaceTable
        .getByRole("textbox", { name: "Workspace display name", exact: true })
        .fill("Fixture workspace");
      await workspaceTable
        .getByRole("textbox", { name: "Workspace API name", exact: true })
        .fill("fixture-workspace");
      await workspaceTable
        .getByRole("button", {
          name: /^Create New (workspace|service API key)$/,
          exact: true,
        })
        .click();
      await pending("workspace");
      await blocked(
        workspaceService.inspector,
        "workspace pending",
        method,
        "Wait for each workspace or key request to finish before you close this service.",
      );
      await finish("workspace", true);
      assert.deepEqual(
        await page.evaluate(() => window.fixture.readWorkspaces()),
        { items: [] },
      );
      await expect(
        workspaceTable.getByRole("textbox", {
          name: "Workspace display name",
          exact: true,
        }),
      ).toHaveValue("Fixture workspace");
      await workspaceTable
        .getByRole("button", { name: "Create New workspace", exact: true })
        .click();
      await pending("workspace");
      await finish("workspace");
      assert.equal(
        (await page.evaluate(() => window.fixture.readWorkspaces())).items[0]
          .display_name,
        "Fixture workspace",
      );
      await expect(
        workspaceTable
          .locator(
            '[data-editable-table-row="fixture-workspace"][data-editable-table-cell="display-name"]',
          )
          .filter({ visible: true }),
      ).toHaveText("Fixture workspace");
      await closeNormally(
        workspaceService.inspector,
        workspaceService.opener,
        method,
      );

      for (const removal of ["none", "pending", "shown", "empty", "failed"]) {
        await load("service");
        const keyService = await openService();
        await keyService.inspector
          .getByRole("button", { name: "Create key", exact: true })
          .click();
        const keyTable = keyService.inspector.locator(
          '[aria-label="Service API keys for Fixture service editor"]',
        );
        await keyTable
          .getByRole("textbox", { name: "Key name", exact: true })
          .fill("Synthetic fixture key");
        await keyTable
          .getByRole("button", {
            name: "Create New service API key",
            exact: true,
          })
          .click();
        await pending("key");
        let protectedInspector = keyService.inspector;
        if (["pending", "empty", "failed"].includes(removal)) {
          await page.evaluate(
            (empty) => window.fixture.removeService(empty),
            removal === "empty",
          );
          protectedInspector = page.getByRole("dialog", {
            name: "fixture",
            exact: true,
          });
          await expect(
            protectedInspector.getByText("The service record is unavailable", {
              exact: true,
            }),
          ).toBeVisible();
        }
        await blocked(
          protectedInspector,
          `key pending (${removal})`,
          method,
          "Wait for the key request to finish before you close this service.",
        );
        assert.equal(
          await page.evaluate(() => window.fixture.pending.has("key")),
          true,
        );
        if (removal === "failed") {
          await finish("key", true);
          await expect(protectedInspector).toHaveCount(0);
          await exactFocus(
            removal === "empty"
              ? page.getByRole("button", {
                  name: "Create service",
                  exact: true,
                })
              : page.locator('[data-service-api-name="remaining"]'),
          );
          continue;
        }
        await finish("key");
        await expect(
          protectedInspector.getByRole("button", {
            name: "Clear key",
            exact: true,
          }),
        ).toBeVisible();
        if (removal === "shown") {
          await page.evaluate(() => window.fixture.removeService());
          protectedInspector = page.getByRole("dialog", {
            name: "fixture",
            exact: true,
          });
          await expect(
            protectedInspector.getByText("The service record is unavailable", {
              exact: true,
            }),
          ).toBeVisible();
        }
        await blocked(
          protectedInspector,
          `key shown (${removal})`,
          method,
          "Copy and clear the one-time key before you close this service.",
        );
        await expect(
          protectedInspector.getByText("synthetic-close-test-key", {
            exact: true,
          }),
        ).toBeVisible();
        assert.equal(
          await page.evaluate(
            () =>
              window.fixture.calls.filter((call) => call.name === "key").length,
          ),
          1,
        );
        const clearKey = protectedInspector.getByRole("button", {
          name: "Clear key",
          exact: true,
        });
        await clearKey.click();
        await expect(
          page.getByText("synthetic-close-test-key", { exact: true }),
        ).toHaveCount(0);
        if (removal === "none")
          await closeNormally(protectedInspector, keyService.opener, method);
        else {
          await expect(protectedInspector).toHaveCount(0);
          await exactFocus(
            removal === "empty"
              ? page.getByRole("button", {
                  name: "Create service",
                  exact: true,
                })
              : page.locator('[data-service-api-name="remaining"]'),
          );
        }
      }
    }
  }

  assert.deepEqual(errors, [], "The browser must not report uncaught errors");
  assert.deepEqual(
    failures,
    [],
    "Pending inspector closure must preserve native dialog state and focus",
  );
  console.log(
    "Inspector close guards passed in split, overlay, and sheet modes.",
  );
} finally {
  await context.close();
  await browser.close();
}
