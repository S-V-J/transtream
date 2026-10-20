# Source this at the top of EVERY script that touches Django in this repo.
#   source scripts/_venv_guard.sh
# Rationale: at 22:06 a ladder run executed under /home/ML/dirmile/venv and
# died on ModuleNotFoundError: daphne. That was LOUD and therefore survivable.
# A wrong venv that happens to contain django would have been SILENT and could
# have written to the wrong database. Two projects share this home directory;
# activation is not reliable enough to trust.
_expected_venv="/home/ML/transtream/venv"
if [ "${VIRTUAL_ENV:-none}" != "$_expected_venv" ]; then
  if [ -d "$_expected_venv" ]; then
    # shellcheck disable=SC1091
    source "$_expected_venv/bin/activate"
  fi
fi
if [ "${VIRTUAL_ENV:-none}" != "$_expected_venv" ]; then
  echo "ABORT: active venv is '${VIRTUAL_ENV:-none}', expected $_expected_venv" >&2
  return 126 2>/dev/null || exit 126
fi
python -c 'import daphne, channels, faster_whisper, redis' 2>/dev/null || {
  echo "ABORT: venv is correct but dependencies are missing" >&2
  return 126 2>/dev/null || exit 126
}
echo "VENV_OK: $VIRTUAL_ENV"
