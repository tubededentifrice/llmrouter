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

export function uniqueDraftRowId(
  existingIds: readonly string[],
  prefix: string,
): string {
  const existing = new Set(existingIds);
  let candidate = prefix;
  while (existing.has(candidate)) candidate += "_";
  return candidate;
}

export type KeyCreationLifecycle =
  | {
      readonly phase: "pending";
      readonly serviceApiName: string;
    }
  | {
      readonly phase: "shown";
      readonly secret: string;
      readonly serviceApiName: string;
    };

export type KeyCreationLifecycleAction =
  | { readonly type: "begin"; readonly serviceApiName: string }
  | {
      readonly type: "created";
      readonly secret: string;
      readonly serviceApiName: string;
    }
  | { readonly type: "failed"; readonly serviceApiName: string }
  | { readonly type: "clear" };

export function serviceInteractionLocked(
  busy: boolean,
  accessPendingCount: number,
  lifecycle: KeyCreationLifecycle | null,
): boolean {
  return busy || accessPendingCount > 0 || lifecycle !== null;
}

export function protectedServiceApiName(
  selectedService: string,
  lifecycle: KeyCreationLifecycle | null,
): string {
  return lifecycle?.serviceApiName ?? selectedService;
}

export function reduceKeyCreationLifecycle(
  state: KeyCreationLifecycle | null,
  action: KeyCreationLifecycleAction,
): KeyCreationLifecycle | null {
  if (action.type === "begin")
    return (
      state ?? {
        phase: "pending",
        serviceApiName: action.serviceApiName,
      }
    );
  if (action.type === "clear") return state?.phase === "shown" ? null : state;
  if (
    state?.phase !== "pending" ||
    state.serviceApiName !== action.serviceApiName
  )
    return state;
  if (action.type === "failed") return null;
  return {
    phase: "shown",
    secret: action.secret,
    serviceApiName: action.serviceApiName,
  };
}
