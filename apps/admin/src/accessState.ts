import type { ServiceKey, Workspace } from "./api.js";

export interface AccessScopeState {
  readonly keys: readonly ServiceKey[];
  readonly phase: "loading" | "ready" | "error";
  readonly secret: string | null;
  readonly service: string;
  readonly workspaces: readonly Workspace[];
}

export type AccessScopeAction =
  | { readonly type: "begin"; readonly service: string }
  | { readonly type: "refresh"; readonly service: string }
  | {
      readonly type: "success";
      readonly service: string;
      readonly keys: readonly ServiceKey[];
      readonly workspaces: readonly Workspace[];
    }
  | { readonly type: "failure"; readonly service: string }
  | {
      readonly type: "show-secret";
      readonly service: string;
      readonly secret: string;
    }
  | { readonly type: "clear-secret"; readonly service: string };

export function initialAccessScopeState(service: string): AccessScopeState {
  return {
    keys: [],
    phase: service === "" ? "ready" : "loading",
    secret: null,
    service,
    workspaces: [],
  };
}

export function reduceAccessScopeState(
  state: AccessScopeState,
  action: AccessScopeAction,
): AccessScopeState {
  if (action.type === "begin") return initialAccessScopeState(action.service);
  if (action.service !== state.service) return state;
  if (action.type === "refresh") return { ...state, phase: "loading" };
  if (action.type === "success")
    return {
      ...state,
      keys: action.keys,
      phase: "ready",
      workspaces: action.workspaces,
    };
  if (action.type === "failure")
    return { ...state, keys: [], phase: "error", workspaces: [] };
  if (action.type === "show-secret") return { ...state, secret: action.secret };
  return { ...state, secret: null };
}

export interface ScopeLoadGuard {
  readonly begin: () => number;
  readonly invalidate: () => void;
  readonly isCurrent: (generation: number) => boolean;
}

export function createScopeLoadGuard(): ScopeLoadGuard {
  let current = 0;
  return {
    begin() {
      current += 1;
      return current;
    },
    invalidate() {
      current += 1;
    },
    isCurrent(generation) {
      return generation === current;
    },
  };
}
