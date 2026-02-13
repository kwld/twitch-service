#!/usr/bin/env bash
set -euo pipefail

ENV_FILE=".env"
ENV_EXAMPLE_FILE=".env.example"

REQUIRED_KEYS=(
  "TWITCH_CLIENT_ID"
  "TWITCH_CLIENT_SECRET"
  "TWITCH_REDIRECT_URI"
  "TWITCH_EVENTSUB_WEBHOOK_CALLBACK_URL"
  "ADMIN_API_KEY"
  "SERVICE_SIGNING_SECRET"
)

get_env_value() {
  local key="$1"
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo ""
    return
  fi
  grep -E "^${key}=" "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true
}

is_missing_or_placeholder() {
  local value="$1"
  if [[ -z "${value}" ]]; then
    return 0
  fi
  if [[ "${value}" == replace_me* ]]; then
    return 0
  fi
  return 1
}

print_env_guidance() {
  local missing_keys=("$@")
  echo "Environment setup required in ${ENV_FILE}:"
  for key in "${missing_keys[@]}"; do
    echo "  - ${key}"
  done
  echo "Update ${ENV_FILE} with real values (use ${ENV_EXAMPLE_FILE} as reference)."
}

if [[ ! -f "${ENV_FILE}" ]]; then
  if [[ ! -f "${ENV_EXAMPLE_FILE}" ]]; then
    echo "Missing ${ENV_EXAMPLE_FILE}; cannot bootstrap ${ENV_FILE}." >&2
    exit 1
  fi
  cp "${ENV_EXAMPLE_FILE}" "${ENV_FILE}"
  echo "Created ${ENV_FILE} from ${ENV_EXAMPLE_FILE}"
fi

missing=()
for key in "${REQUIRED_KEYS[@]}"; do
  value="$(get_env_value "${key}")"
  if is_missing_or_placeholder "${value}"; then
    missing+=("${key}")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  print_env_guidance "${missing[@]}"
fi

PYTHON_BIN=""
if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "Python is required but neither 'python' nor 'python3' was found." >&2
  exit 1
fi

"${PYTHON_BIN}" -m pip install --upgrade pip
"${PYTHON_BIN}" -m pip install -e .

if [[ -f "test-app/package.json" ]]; then
  (
    cd test-app
    npm install
  )
fi

echo "Install complete."
