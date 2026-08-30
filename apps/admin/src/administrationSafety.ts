import type { ScopeLoadGuard } from "./accessState.ts";

export function expireAdministratorSessionLoads(
  globalLoadGuard: ScopeLoadGuard,
  scopeLoadGuard: ScopeLoadGuard,
  clearSession: () => void,
): void {
  globalLoadGuard.invalidate();
  scopeLoadGuard.invalidate();
  clearSession();
}

export function invalidateRetainedMediaLoad(
  loadGuard: ScopeLoadGuard,
  objectUrl: string | null,
  revokeObjectUrl: (url: string) => void,
): null {
  loadGuard.invalidate();
  if (objectUrl !== null) revokeObjectUrl(objectUrl);
  return null;
}

export async function updateRetentionDuration(
  currentDays: number,
  nextDays: number,
  confirm: (message: string) => boolean,
  write: (days: number) => Promise<unknown>,
): Promise<boolean> {
  if (
    nextDays < currentDays &&
    !confirm(
      `Lower global retention from ${String(currentDays)} to ${String(nextDays)} days? Detailed logs, activity, uploaded images, and retained generated media will become eligible for earlier deletion.`,
    )
  )
    return false;
  await write(nextDays);
  return true;
}
