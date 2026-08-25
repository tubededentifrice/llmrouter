import type { Price } from "./api.js";

function opaqueFormValue(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value : "";
}

export function credentialFormValue(form: FormData): {
  readonly apiName: string;
  readonly secret: string;
} {
  return {
    apiName: opaqueFormValue(form, "credential_api_name").trim(),
    secret: opaqueFormValue(form, "secret"),
  };
}

export const usageUnits = [
  "input_token",
  "output_token",
  "cached_input_token",
  "image",
  "video_second",
  "audio_second",
  "request",
  "provider_unit",
] as const;

export function parseManualPrice(
  currencyValue: string,
  unitPriceValue: string,
): Price | null {
  const currency = currencyValue.trim().toUpperCase();
  const text = unitPriceValue.trim();
  if (currency === "" && text === "") return null;
  if (currency === "")
    throw new Error("Enter the three-letter currency for the manual price.");
  if (!/^[A-Z]{3}$/.test(currency))
    throw new Error("Use a three-letter uppercase currency code.");
  if (text === "")
    throw new Error(
      "Enter at least one typed unit amount for the manual price.",
    );
  const entries = text.split(/[\n,]/).flatMap((item) => {
    const trimmed = item.trim();
    return trimmed === "" ? [] : [trimmed];
  });
  if (entries.length === 0)
    throw new Error(
      "Enter at least one typed unit amount for the manual price.",
    );
  if (entries.length > 16)
    throw new Error("Enter no more than 16 typed unit amounts.");
  const seen = new Set<string>();
  const unit_prices = entries.map((entry) => {
    const separator = entry.indexOf("=");
    const unit = separator < 0 ? entry : entry.slice(0, separator).trim();
    const amount = separator < 0 ? "" : entry.slice(separator + 1).trim();
    if (!usageUnits.some((candidate) => candidate === unit))
      throw new Error(
        `Use a supported price unit. Supported units: ${usageUnits.join(", ")}.`,
      );
    const typedUnit = unit as (typeof usageUnits)[number];
    if (seen.has(typedUnit))
      throw new Error(`Enter the ${typedUnit} price only once.`);
    if (!/^[0-9]+(?:\.[0-9]+)?$/.test(amount))
      throw new Error(`Enter a fixed-decimal amount for ${typedUnit}.`);
    seen.add(typedUnit);
    return { unit: typedUnit, amount };
  });
  return { currency, unit_prices };
}

const supportedImageTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maximumInputImageBytes = 20_971_520;
const maximumInputImageTotalBytes = 52_428_800;
const maximumInputImages = 8;

export function validateInputImageSelection(
  current: readonly { readonly sizeBytes: number }[],
  added: readonly { readonly size: number; readonly type: string }[],
): void {
  if (current.length + added.length > maximumInputImages)
    throw new Error(
      "Add no more than 8 input images. Remove an image and try again.",
    );
  for (const file of added) {
    if (!supportedImageTypes.has(file.type))
      throw new Error("Use a JPEG, PNG, or WebP image.");
    if (file.size < 1)
      throw new Error("Each input image must contain at least 1 byte.");
    if (file.size > maximumInputImageBytes)
      throw new Error("Each input image must be 20,971,520 bytes or smaller.");
  }
  const totalBytes = [...current, ...added].reduce(
    (total, file) => total + ("sizeBytes" in file ? file.sizeBytes : file.size),
    0,
  );
  if (totalBytes > maximumInputImageTotalBytes)
    throw new Error(
      "Input images must total 52,428,800 bytes or less. Remove an image and try again.",
    );
}

interface InputImageFile {
  readonly size: number;
  readonly type: string;
}

export interface InputImageSelectionQueue<T> {
  readonly add: <F extends InputImageFile>(
    files: readonly F[],
    read: (file: F) => Promise<T>,
  ) => Promise<readonly T[]>;
  readonly remove: (matches: (image: T) => boolean) => readonly T[];
}

export function createInputImageSelectionQueue<
  T extends { readonly sizeBytes: number },
>(
  initial: readonly T[],
  onChange: (images: readonly T[]) => void,
): InputImageSelectionQueue<T> {
  let current = [...initial];
  let pending: Promise<void> = Promise.resolve();
  return {
    add(files, read) {
      const operation = pending.then(async () => {
        validateInputImageSelection(current, files);
        const added = await Promise.all(files.map(read));
        current = [...current, ...added];
        onChange(current);
        return current;
      });
      pending = operation.then(
        () => undefined,
        () => undefined,
      );
      return operation;
    },
    remove(matches) {
      current = current.filter((image) => !matches(image));
      onChange(current);
      return current;
    },
  };
}
