#!/usr/bin/env bash
# ==========================================================================
# test_daemon.sh — Integration tests for the Lab Data Mount Daemon
# ==========================================================================
#
# Usage:
#   sudo bash test_daemon.sh
#
# Prerequisites:
#   - The daemon must be running:  sudo python3 daemon.py
#   - curl and jq must be installed
# ==========================================================================

set -euo pipefail

BASE_URL="http://127.0.0.1:5000"
USER="testuser"
TOOL="microscope1"
PASS=0
FAIL=0

# --- helpers --------------------------------------------------------------

green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

assert_status() {
    local label="$1" expected="$2" actual="$3"
    if [ "$actual" -eq "$expected" ]; then
        green "  ✓ $label (HTTP $actual)"
        (( PASS++ )) || true
    else
        red "  ✗ $label — expected HTTP $expected, got $actual"
        (( FAIL++ )) || true
    fi
}

assert_json_field() {
    local label="$1" body="$2" field="$3" expected="$4"
    local actual
    actual=$(echo "$body" | jq -r ".$field")
    if [ "$actual" = "$expected" ]; then
        green "  ✓ $label ($field=$actual)"
        (( PASS++ )) || true
    else
        red "  ✗ $label — $field: expected '$expected', got '$actual'"
        (( FAIL++ )) || true
    fi
}

# --- pre-flight -----------------------------------------------------------

bold "=== Lab Data Mount Daemon — Test Suite ==="
echo ""

# 1. Health check
bold "[1/7] Health check"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
assert_status "GET /health" 200 "$HTTP_CODE"
echo ""

# --- mount tests ----------------------------------------------------------

# 2. Mount (success)
bold "[2/7] Mount — valid request"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/mount" \
    -H "Content-Type: application/json" \
    -d "{\"user\": \"$USER\", \"tool\": \"$TOOL\"}")
BODY=$(echo "$RESPONSE" | head -n -1)
CODE=$(echo "$RESPONSE" | tail -n 1)
assert_status "POST /mount" 201 "$CODE"
assert_json_field "Response status" "$BODY" "status" "ok"
echo ""

# 3. Verify bind mount exists
bold "[3/7] Verify bind mount"
if mount | grep -q "/tmp/labdata/sessions/$TOOL/$USER"; then
    green "  ✓ mount point visible in 'mount' output"
    (( PASS++ )) || true
else
    red "  ✗ mount point NOT found in 'mount' output"
    (( FAIL++ )) || true
fi
echo ""

# 4. Idempotent mount (already mounted)
bold "[4/7] Mount — idempotent (already mounted)"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/mount" \
    -H "Content-Type: application/json" \
    -d "{\"user\": \"$USER\", \"tool\": \"$TOOL\"}")
BODY=$(echo "$RESPONSE" | head -n -1)
CODE=$(echo "$RESPONSE" | tail -n 1)
assert_status "POST /mount (idempotent)" 200 "$CODE"
assert_json_field "Already mounted message" "$BODY" "message" "Already mounted"
echo ""

# --- unmount tests --------------------------------------------------------

# 5. Unmount (success)
bold "[5/7] Unmount — valid request"
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/unmount" \
    -H "Content-Type: application/json" \
    -d "{\"user\": \"$USER\", \"tool\": \"$TOOL\"}")
BODY=$(echo "$RESPONSE" | head -n -1)
CODE=$(echo "$RESPONSE" | tail -n 1)
assert_status "POST /unmount" 200 "$CODE"
assert_json_field "Response status" "$BODY" "status" "ok"
echo ""

# 6. Verify mount is gone
bold "[6/7] Verify mount removed"
if mount | grep -q "/tmp/labdata/sessions/$TOOL/$USER"; then
    red "  ✗ mount point still visible — unmount failed"
    (( FAIL++ )) || true
else
    green "  ✓ mount point removed"
    (( PASS++ )) || true
fi
echo ""

# --- error handling tests -------------------------------------------------

# 7. Error cases
bold "[7/7] Error handling"

# Missing fields
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/mount" \
    -H "Content-Type: application/json" \
    -d '{"user": "testuser"}')
CODE=$(echo "$RESPONSE" | tail -n 1)
assert_status "Missing 'tool' → 400" 400 "$CODE"

# Invalid JSON
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/mount" \
    -H "Content-Type: application/json" \
    -d 'not json')
CODE=$(echo "$RESPONSE" | tail -n 1)
assert_status "Invalid JSON → 400" 400 "$CODE"

# Path traversal attempt
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/mount" \
    -H "Content-Type: application/json" \
    -d '{"user": "../etc", "tool": "evil"}')
CODE=$(echo "$RESPONSE" | tail -n 1)
assert_status "Path traversal → 400" 400 "$CODE"

# Unmount non-existent path
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/unmount" \
    -H "Content-Type: application/json" \
    -d '{"user": "nobody", "tool": "nothing"}')
CODE=$(echo "$RESPONSE" | tail -n 1)
assert_status "Unmount missing path → 404" 404 "$CODE"

echo ""

# --- summary --------------------------------------------------------------

bold "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
    red "SOME TESTS FAILED"
    exit 1
else
    green "ALL TESTS PASSED"
    exit 0
fi
