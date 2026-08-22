import { AdministrationApiError, errorMessage } from "./api.js";
import type { Notice } from "./App.js";

function errorNotice(error: unknown): Notice {
  return {
    tone: "error",
    message: errorMessage(error),
    staleRevision:
      error instanceof AdministrationApiError && error.staleRevision,
  };
}

export async function recoverAfterMutationFailure(
  error: unknown,
  onChanged: () => Promise<void>,
  onNotice: (notice: Notice) => void,
): Promise<void> {
  const notice = errorNotice(error);
  onNotice(notice);
  if (
    !(error instanceof AdministrationApiError) ||
    (!error.staleRevision && !error.outcomeUncertain)
  ) {
    return;
  }
  try {
    await onChanged();
  } catch (refreshError) {
    onNotice({
      ...notice,
      message: `${notice.message} Current data did not refresh. ${errorMessage(refreshError)}`,
    });
  }
}
