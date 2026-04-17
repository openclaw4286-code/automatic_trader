#!/usr/bin/env bash
# Run every test file in /tests and summarise.
set -uo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH=.

fail=0
for t in tests/test_*.py; do
  echo "=== $t"
  if ! python "$t"; then
    fail=1
    echo "!! FAILED: $t"
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "✅ all tests passed"
else
  echo "❌ test failures above"
  exit 1
fi
