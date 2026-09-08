import { act } from "react";
import { create } from "react-test-renderer";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  Button,
  EditableTable,
  SecretRevealPanel,
  type EditableTableProps,
  type SecretRevealPanelProps,
} from "@opendle/ui";
import {
  ServiceAccess,
  type ServiceAccessProps,
} from "../src/ServiceAccess.tsx";
import {
  createAdministrationClient,
  type Service,
  type Workspace,
  type ServiceKey,
} from "../src/api.ts";

vi.mock("@opendle/ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("@opendle/ui")>();
  return {
    ...original,
    EditableTable: () => null,
    SecretRevealPanel: () => null,
  };
});

const service: Service = {
  api_name: "alpha",
  display_name: "Alpha",
  created_at: "2026-08-25T00:00:00Z",
};
const workspace: Workspace = {
  api_name: "billing",
  display_name: "Billing",
  created_at: service.created_at,
};
const key: ServiceKey = {
  id: "key-1",
  name: "Backend",
  created_at: service.created_at,
  last_used_at: null,
};
const page = <T,>(items: readonly T[]) => ({
  items,
  page: { has_more: false, next_cursor: null },
});
interface WorkspaceDraft {
  apiName: string;
  displayName: string;
  createdAt: string | null;
}
interface KeyDraft {
  name: string;
  createdAt: string | null;
  lastUsedAt: string | null | undefined;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}
// The installed renderer permits bounded hook tests without a browser dependency.
// eslint-disable-next-line @typescript-eslint/no-deprecated -- This test uses the repository's existing renderer.
let renderer: ReturnType<typeof create>;
function required<T>(value: T | undefined): T {
  if (value === undefined) throw new Error("Missing test element.");
  return value;
}
async function flush(action: () => void | Promise<void>): Promise<void> {
  await act(async () => {
    await action();
  });
}
let props: ServiceAccessProps;
function workspaceTable(): EditableTableProps<WorkspaceDraft> {
  return required(renderer.root.findAllByType(EditableTable)[0])
    .props as EditableTableProps<WorkspaceDraft>;
}
function keyTable(): EditableTableProps<KeyDraft> {
  return required(renderer.root.findAllByType(EditableTable)[1])
    .props as EditableTableProps<KeyDraft>;
}
async function click(label: string) {
  const button = renderer.root
    .findAllByType(Button)
    .find((item) => item.props.children === label);
  expect(button).toBeDefined();
  const action = required(button).props.onClick as () => void;
  await flush(() => {
    action();
  });
}
async function mount(overrides: Partial<ServiceAccessProps> = {}) {
  await flush(() => {
    // eslint-disable-next-line @typescript-eslint/no-deprecated -- This test uses the repository's existing renderer.
    renderer = create(<ServiceAccess {...props} {...overrides} />);
  });
}
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const client = createAdministrationClient(vi.fn());
  vi.spyOn(client, "workspaces").mockResolvedValue(page([workspace]));
  vi.spyOn(client, "keys").mockResolvedValue(page([key]));
  props = {
    client,
    csrf: "csrf",
    serviceApiName: "alpha",
    service,
    serviceAvailable: true,
    serviceFresh: true,
    hostBlocked: false,
    onNotice: vi.fn(),
    onPendingChange: vi.fn(),
    onDirtyChange: vi.fn(),
    onSecretStateChange: vi.fn(),
  };
});
afterEach(async () => {
  await flush(() => {
    renderer.unmount();
  });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("service-details workspace and key access", () => {
  it("offers row creation actions only while a draft exists", async () => {
    await mount();
    expect(workspaceTable().onCreate).toBeUndefined();
    expect(workspaceTable().onCancel).toBeUndefined();
    expect(keyTable().onCreate).toBeUndefined();
    expect(keyTable().onCancel).toBeUndefined();
    await click("Create workspace");
    expect(workspaceTable().onCreate).toBeTypeOf("function");
    expect(workspaceTable().onCancel).toBeTypeOf("function");
    await flush(() => {
      workspaceTable().onCancel?.("new");
    });
    expect(workspaceTable().rows).toHaveLength(1);
    expect(workspaceTable().onCreate).toBeUndefined();
    expect(workspaceTable().onCancel).toBeUndefined();
    await click("Create key");
    expect(keyTable().onCreate).toBeTypeOf("function");
    expect(keyTable().onCancel).toBeTypeOf("function");
    vi.spyOn(props.client, "createKey").mockResolvedValueOnce({
      key: { ...key, id: "key-2" },
      secret: "synthetic-row-action-test-value",
    });
    await flush(async () => {
      await keyTable().onCreate?.("new", {
        name: "Deployment",
        createdAt: null,
        lastUsedAt: null,
      });
    });
    expect(keyTable().rows).toHaveLength(2);
    expect(keyTable().onCreate).toBeUndefined();
    expect(keyTable().onCancel).toBeUndefined();
    expect(renderer.root.findAllByType(SecretRevealPanel)).toHaveLength(1);
  });

  it("loads only the exact route and does not load an unconfirmed service", async () => {
    await mount({ service: null, serviceAvailable: false });
    expect(vi.spyOn(props.client, "workspaces")).not.toHaveBeenCalled();
    expect(vi.spyOn(props.client, "keys")).not.toHaveBeenCalled();
    await flush(() => {
      renderer.update(<ServiceAccess {...props} />);
    });
    expect(vi.spyOn(props.client, "workspaces")).toHaveBeenCalledWith("alpha");
    expect(vi.spyOn(props.client, "keys")).toHaveBeenCalledWith("alpha");
    expect(workspaceTable().rows[0]?.draft.apiName).toBe("billing");
  });
  it("retains stale workspace rows and blocks only workspace writes until retry succeeds", async () => {
    await mount();
    vi.spyOn(props.client, "workspaces").mockRejectedValueOnce(
      new Error("offline"),
    );
    await click("Refresh workspaces");
    expect(workspaceTable().state).toMatchObject({
      kind: "error",
    });
    expect(workspaceTable().rows[0]?.locked).toBe(true);
    for (const field of ["stale", "editing", "dirty", "isNew"]) {
      expect(workspaceTable().rows[0]).not.toHaveProperty(field);
    }
    expect(workspaceTable().state?.message).toContain("stale");
    expect(keyTable().rows[0]?.locked).toBe(false);
    await expect(
      workspaceTable().onDelete?.(
        "billing",
        required(workspaceTable().rows[0]),
      ),
    ).rejects.toThrow();
    await flush(async () => {
      const state = workspaceTable().state;
      if (state?.kind === "error") await state.onRetry?.();
    });
    expect(workspaceTable().rows[0]?.locked).toBe(false);
  });
  it("retains stale key rows without disabling workspace writes", async () => {
    await mount();
    vi.spyOn(props.client, "keys").mockRejectedValueOnce(new Error("offline"));
    await click("Refresh keys");
    expect(keyTable().rows[0]?.locked).toBe(true);
    for (const field of ["stale", "editing", "dirty", "isNew"]) {
      expect(keyTable().rows[0]).not.toHaveProperty(field);
    }
    expect(keyTable().state?.message).toContain("stale");
    expect(workspaceTable().rows[0]?.locked).toBe(false);
  });
  it("keeps failed create drafts and reports only entered values as dirty", async () => {
    await mount();
    await click("Create key");
    expect(props.onDirtyChange).toHaveBeenLastCalledWith(false);
    await flush(() => {
      keyTable().onDraftChange("new", { name: "Deployment" });
    });
    expect(props.onDirtyChange).toHaveBeenLastCalledWith(true);
    vi.spyOn(props.client, "createKey").mockRejectedValueOnce(
      new Error("Name exists."),
    );
    await flush(async () => {
      await expect(
        keyTable().onCreate?.("new", {
          name: "Deployment",
          createdAt: null,
          lastUsedAt: null,
        }),
      ).rejects.toThrow(
        "The Router could not complete the operation. Try again.",
      );
    });
    expect(keyTable().rows).toHaveLength(2);
    expect(keyTable().rows[1]?.draft.name).toBe("Deployment");
    expect(keyTable().onCreate).toBeTypeOf("function");
    expect(keyTable().onCancel).toBeTypeOf("function");
    expect(renderer.root.findAllByType(SecretRevealPanel)).toHaveLength(0);
    expect(props.onPendingChange).toHaveBeenLastCalledWith(0);
  });
  it("retains a create response secret after service absence, then clears it", async () => {
    await mount();
    const pending = deferred<{ key: ServiceKey; secret: string }>();
    vi.spyOn(props.client, "createKey").mockReturnValue(pending.promise);
    await click("Create key");
    let request: Promise<unknown> | undefined;
    await flush(() => {
      request = Promise.resolve(
        keyTable().onCreate?.("new", {
          name: "Deployment",
          createdAt: null,
          lastUsedAt: null,
        }),
      );
    });
    expect(props.onPendingChange).toHaveBeenLastCalledWith(1);
    await flush(() => {
      renderer.update(
        <ServiceAccess
          {...props}
          service={null}
          serviceAvailable={false}
          serviceFresh={false}
        />,
      );
    });
    await flush(async () => {
      pending.resolve({
        key: { ...key, id: "key-2" },
        secret: "synthetic-one-time-test-value",
      });
      await request;
    });
    const reveal = renderer.root.findByType(SecretRevealPanel)
      .props as SecretRevealPanelProps;
    expect(reveal.copyLabel).toBe("Copy secret");
    expect(reveal.dismissLabel).toBe("Clear secret");
    expect(props.onSecretStateChange).toHaveBeenLastCalledWith(true);
    expect(props.onPendingChange).toHaveBeenLastCalledWith(0);
    await flush(() => {
      reveal.onDismiss?.();
    });
    expect(renderer.root.findAllByType(SecretRevealPanel)).toHaveLength(0);
    expect(props.onSecretStateChange).toHaveBeenLastCalledWith(false);
  });
  it("does not remove metadata before confirmed revoke and retains it after failure", async () => {
    await mount();
    const pending = deferred<undefined>();
    vi.spyOn(props.client, "revokeKey")
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(undefined);
    let request: Promise<unknown> | undefined;
    await flush(() => {
      request = Promise.resolve(
        keyTable().onDelete?.("key-1", required(keyTable().rows[0])),
      );
    });
    expect(keyTable().rows).toHaveLength(1);
    await flush(async () => {
      pending.reject(new Error("offline"));
      await expect(request).rejects.toThrow();
    });
    expect(keyTable().rows).toHaveLength(1);
    await flush(async () => {
      await keyTable().onDelete?.("key-1", required(keyTable().rows[0]));
    });
    expect(keyTable().rows).toHaveLength(0);
    expect(vi.spyOn(props.client, "revokeKey")).toHaveBeenCalledWith(
      "alpha",
      "key-1",
      "csrf",
    );
  });
  it("ignores reads from a previous service route", async () => {
    const delayed = deferred<ReturnType<typeof page<Workspace>>>();
    vi.spyOn(props.client, "workspaces")
      .mockReturnValueOnce(delayed.promise)
      .mockResolvedValueOnce(
        page([{ ...workspace, api_name: "beta-workspace" }]),
      );
    await mount();
    await flush(() => {
      renderer.update(
        <ServiceAccess
          {...props}
          serviceApiName="beta"
          service={{ ...service, api_name: "beta" }}
        />,
      );
    });
    await flush(() => {
      delayed.resolve(page([workspace]));
    });
    expect(workspaceTable().rows[0]?.draft.apiName).toBe("beta-workspace");
  });
  it("blocks all access writes when the service record is stale", async () => {
    await mount({ serviceFresh: false });
    expect(workspaceTable().rows[0]?.locked).toBe(true);
    expect(keyTable().rows[0]?.locked).toBe(true);
  });
  it("blocks access writes during a service form write without blocking reads", async () => {
    await mount();
    await click("Create workspace");
    await flush(() => {
      renderer.update(<ServiceAccess {...props} hostBlocked />);
    });
    expect(workspaceTable().rows[0]?.locked).toBe(true);
    expect(keyTable().rows[0]?.locked).toBe(true);
    await expect(
      workspaceTable().onCreate?.("new", {
        apiName: "new",
        displayName: "New",
        createdAt: null,
      }),
    ).rejects.toThrow();
    await click("Refresh keys");
    expect(vi.spyOn(props.client, "keys")).toHaveBeenCalledTimes(2);
  });
});
