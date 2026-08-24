import {
  AdministrationApiError,
  type MediaJob,
  type RuntimeClient,
} from "./api.js";

export async function waitForMediaJob(
  client: Pick<RuntimeClient, "mediaJob">,
  current: MediaJob,
  remainingPolls = 120,
  wait: () => Promise<void> = () =>
    new Promise((resolve) => globalThis.setTimeout(resolve, 1000)),
): Promise<MediaJob> {
  if (current.state !== "pending" && current.state !== "running")
    return current;
  if (remainingPolls <= 0)
    throw new AdministrationApiError(
      408,
      "media_job_poll_timeout",
      `Media job ${current.id} is still ${current.state}.`,
      {
        reason: `The playground stopped polling job ${current.id}. The job can still complete. Do not submit the same work again. Query /v1/media-jobs/${current.id} with the same service key.`,
      },
    );
  await wait();
  return waitForMediaJob(
    client,
    await client.mediaJob(current.id),
    remainingPolls - 1,
    wait,
  );
}
