#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

cd "${repository_root}"

for workspace in \
  packages/typescript-browser-client \
  packages/typescript-server-client; do
  package_directory="${temporary_directory}/$(basename "${workspace}")"
  unpack_directory="${package_directory}/unpacked"
  mkdir -p "${package_directory}" "${unpack_directory}"
  archive_name="$(npm pack --silent --pack-destination "${package_directory}" --workspace "${workspace}")"
  archive_path="${package_directory}/${archive_name}"
  if [[ ! -f "${archive_path}" ]]; then
    echo "The client package archive is missing: ${workspace}" >&2
    exit 1
  fi
  tar -xzf "${archive_path}" -C "${unpack_directory}"
  entry_path="${unpack_directory}/package/dist/index.js"
  node --input-type=module - "${entry_path}" <<'NODE'
import { pathToFileURL } from "node:url";

const entryPath = process.argv[2];
const client = await import(pathToFileURL(entryPath).href);
if (
  typeof client.validateContract !== "function" ||
  typeof client.ContractValidationError !== "function"
) {
  throw new Error("The packed client does not export contract validation.");
}
NODE
done

echo "Client package checks passed."
