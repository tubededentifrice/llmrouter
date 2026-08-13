#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
items="$(bd -C "${repository_root}" list --all --json --limit 0)"

jq -e '
  . as $items |
  ($items | map({
    key: .id,
    value: {
      main: (((.labels // []) | index("main")) != null),
      review: (((.labels // []) | index("selfreview")) != null),
      blocker: ((.labels // []) | any(startswith("blocker:")))
    }
  }) | from_entries) as $kinds |
  type == "array" and length > 0 and
  ([$items[] | select(
    .issue_type == "epic" and
    (any(.dependencies[]?; .type == "parent-child") | not)
  )] | length == 1) and
  all($items[] | select(((.labels // []) | index("main")) != null);
    (.description | contains("## Acceptance Criteria")) and
    (.description | contains("## Test Plan")) and
    (.description | contains("## Dependencies and Human Input"))) and
  all($items[] | select(((.labels // []) | index("selfreview")) != null);
    (.metadata.review_of // "") as $main_id |
    $kinds[$main_id].main == true) and
  all($items[] | select((.labels // []) | any(startswith("blocker:")));
    .issue_type == "task" and
    (.title | startswith("BLOCKER: ")) and
    (.description | contains("## Responsible Actor")) and
    (.description | contains("## Exit Condition"))) and
  all($items[] | select(((.labels // []) | index("main")) != null);
    (((.labels // []) | index("entrypoint")) != null) or
    any(.dependencies[]?; .type == "blocks")) and
  all($items[] | select(((.labels // []) | index("main")) != null);
    all(.dependencies[]? | select(.type == "blocks");
      $kinds[.depends_on_id].review == true or
      $kinds[.depends_on_id].blocker == true))
' <<<"${items}" >/dev/null

main_ids="$(jq -r '.[] | select((.labels // []) | index("main")) | .id' <<<"${items}")"
while IFS= read -r main_id; do
  [[ -n "${main_id}" ]] || continue
  review_count="$(
    jq --arg main_id "${main_id}" '
      [.[] | select(
        ((.labels // []) | index("selfreview")) and
        (.metadata.review_of // "") == $main_id and
        any(.dependencies[]?; .depends_on_id == $main_id and .type == "blocks")
      )] | length
    ' <<<"${items}"
  )"
  if [[ "${review_count}" != "1" ]]; then
    echo "Main task ${main_id} must have exactly one dependent self-review task." >&2
    exit 1
  fi
done <<<"${main_ids}"

echo "Beads plan checks passed."
