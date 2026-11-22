#!/usr/bin/env bash
set -euo pipefail

# Explicit seed runner. Keep this out of automatic deploys.
# Usage:
#   bash scripts/db/apply_seed.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMMON_LIB="${SCRIPT_DIR}/common.sh"
SEED_SQL="${REPO_ROOT}/sql/seed_entities.sql"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/db/apply_seed.sh

Applies the entity and alias seed SQL. Safe to run repeatedly.
EOF
}

if [[ ! -f "${COMMON_LIB}" ]]; then
  echo "Missing common helper: ${COMMON_LIB}" >&2
  exit 1
fi
source "${COMMON_LIB}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

db_require_file "${SEED_SQL}" "seed SQL"

db_init_runtime "${REPO_ROOT}"
db_ensure_postgres_running

echo "Applying seed SQL..."
db_psql_file "${SEED_SQL}"

echo "Done. Seed SQL applied successfully."
