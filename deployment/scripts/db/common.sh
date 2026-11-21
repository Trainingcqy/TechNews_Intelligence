#!/usr/bin/env bash

# Common helpers for deployment DB scripts.
# shellcheck shell=bash

DB_RUNTIME_READY=0
DB_USER="${DB_USER:-}"
DB_NAME="${DB_NAME:-}"
REPO_ROOT="${REPO_ROOT:-}"
COMPOSE_CMD=()
PYTHON_CMD=()
PYTHON_RUNTIME_MODE="${PYTHON_RUNTIME_MODE:-container}"
PYTHON_SERVICE="${PYTHON_SERVICE:-bot}"

db_die() {
  echo "$*" >&2
  exit 1
}

db_read_env_var() {
  local key="$1"
  local file="$2"
  local raw
  raw="$(
    grep -E "^(export[[:space:]]+)?${key}=" "${file}" | tail -n 1 \
      | sed -E "s/^(export[[:space:]]+)?${key}=//" || true
  )"
  raw="${raw%$'\r'}"

  # Trim leading/trailing spaces.
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"

  if [[ "${raw}" == \"*\" ]]; then
    raw="${raw#\"}"
    raw="${raw%\"}"
  elif [[ "${raw}" == \'*\' ]]; then
    raw="${raw#\'}"
    raw="${raw%\'}"
  else
    # Unquoted value: strip inline comments and trim again.
    raw="${raw%%#*}"
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
  fi

  printf '%s' "${raw}"
}

db_detect_compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
  else
    db_die "Neither 'docker compose' nor 'docker-compose' is available."
  fi
}

db_detect_python_cmd() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    if command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
      PYTHON_CMD=("${PYTHON_BIN}")
      return
    fi
    db_die "PYTHON_BIN is set but not executable or not found: ${PYTHON_BIN}"
  fi

  if [[ -n "${REPO_ROOT:-}" && -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON_CMD=("${REPO_ROOT}/.venv/bin/python")
    return
  fi

  if [[ -n "${REPO_ROOT:-}" && -x "${REPO_ROOT}/.venv/Scripts/python.exe" ]]; then
    PYTHON_CMD=("${REPO_ROOT}/.venv/Scripts/python.exe")
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=(python3)
    return
  fi

  if command -v python >/dev/null 2>&1; then
    PYTHON_CMD=(python)
    return
  fi

  db_die "Python runtime not found. Install python3 or set PYTHON_BIN=/path/to/python."
}

db_assert_python_modules() {
  db_detect_python_cmd
  local modules=("$@")
  local module
  for module in "${modules[@]}"; do
    if ! "${PYTHON_CMD[@]}" -c "import ${module}" >/dev/null 2>&1; then
      db_die "Missing Python module '${module}' for ${PYTHON_CMD[*]}. Run: ${PYTHON_CMD[*]} -m pip install -r requirements.txt"
    fi
  done
}

db_python_run() {
  db_assert_runtime

  if [[ "${PYTHON_RUNTIME_MODE}" == "host" ]]; then
    db_assert_python_modules psycopg2 requests
    "${PYTHON_CMD[@]}" "$@"
    return
  fi

  (
    cd "${REPO_ROOT}"
    "${COMPOSE_CMD[@]}" run --rm -T --no-deps \
      -v "${REPO_ROOT}:/app" \
      -w /app \
      "${PYTHON_SERVICE}" \
      python "$@"
  )
}

db_require_file() {
  local file="$1"
  local label="${2:-SQL file}"
  if [[ ! -f "${file}" ]]; then
    db_die "Missing ${label}: ${file}"
  fi
}

db_init_runtime() {
  local repo_root="$1"
  REPO_ROOT="${repo_root}"

  if [[ ! -f "${REPO_ROOT}/.env" ]]; then
    db_die "Missing .env at ${REPO_ROOT}/.env. Copy .env.example and fill in the database account first."
  fi

  if [[ -z "${DB_USER:-}" ]]; then
    DB_USER="$(db_read_env_var DB_USER "${REPO_ROOT}/.env")"
  fi
  if [[ -z "${DB_NAME:-}" ]]; then
    DB_NAME="$(db_read_env_var DB_NAME "${REPO_ROOT}/.env")"
  fi
  DB_NAME="${DB_NAME:-DB}"

  if [[ -z "${DB_USER:-}" ]]; then
    db_die "DB_USER must be set in .env"
  fi

  db_detect_compose_cmd
  DB_RUNTIME_READY=1
}

db_assert_runtime() {
  if [[ "${DB_RUNTIME_READY}" -ne 1 ]]; then
    db_die "DB runtime is not initialized. Call db_init_runtime first."
  fi
}

db_ensure_postgres_running() {
  db_assert_runtime
  echo "Starting postgres service if needed..."
  (
    cd "${REPO_ROOT}"
    "${COMPOSE_CMD[@]}" up -d postgres >/dev/null
  )
}

db_psql_file() {
  db_assert_runtime
  local sql_file="$1"
  shift || true
  db_require_file "${sql_file}" "SQL file"
  (
    cd "${REPO_ROOT}"
    cat "${sql_file}" | "${COMPOSE_CMD[@]}" exec -T postgres \
      psql -v ON_ERROR_STOP=1 "$@" -U "${DB_USER}" -d "${DB_NAME}"
  )
}

db_psql_sql() {
  db_assert_runtime
  local sql="$1"
  shift || true
  (
    cd "${REPO_ROOT}"
    printf '%s\n' "${sql}" | "${COMPOSE_CMD[@]}" exec -T postgres \
      psql -v ON_ERROR_STOP=1 "$@" -U "${DB_USER}" -d "${DB_NAME}"
  )
}
