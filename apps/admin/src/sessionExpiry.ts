const maximumTimerDelay = 2_147_483_647;

export function scheduleSessionExpiry(
  expiresAt: string,
  onExpire: () => void,
): () => void {
  const deadline = Date.parse(expiresAt);
  let timer: ReturnType<typeof globalThis.setTimeout> | undefined;
  let cancelled = false;
  const schedule = () => {
    if (cancelled) return;
    const remaining = Number.isNaN(deadline) ? 0 : deadline - Date.now();
    if (remaining <= 0) {
      onExpire();
      return;
    }
    timer = globalThis.setTimeout(
      schedule,
      Math.min(remaining, maximumTimerDelay),
    );
  };
  timer = globalThis.setTimeout(schedule, 0);
  return () => {
    cancelled = true;
    if (timer !== undefined) globalThis.clearTimeout(timer);
  };
}
