#!/usr/bin/env bash

# Multi-language Hello World script
# Outputs "hello world" from five different languages, each prefixed with the language name.
# Requires: bash, python3, node, ruby, go

set -o pipefail

errors=0

cleanup() {
  if [[ -n "${TMPDIR:-}" ]]; then
    rm -rf "$TMPDIR"
  fi
}
trap cleanup EXIT

check_runtime() {
  local name=$1 cmd=$2
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: $name ($cmd) not found. Please install it." >&2
    errors=1
  fi
}

# Verify all runtimes are available
check_runtime "Python" "python3"
check_runtime "Node.js" "node"
check_runtime "Ruby" "ruby"
check_runtime "Go" "go"

# Exit early if any runtime is missing
if [[ $errors -ne 0 ]]; then
  exit 1
fi

# Create a temporary directory for the Go program
TMPDIR=$(mktemp -d)
cat > "$TMPDIR/main.go" <<'EOF'
package main

import "fmt"

func main() {
	fmt.Println("[Go] hello world")
}
EOF

fail=0

# 1. Bash
echo "[Bash] hello world" || fail=1

# 2. Python
python3 -c "print('[Python] hello world')" || fail=1

# 3. JavaScript (Node.js)
node -e "console.log('[JavaScript] hello world')" || fail=1

# 4. Ruby
ruby -e "puts '[Ruby] hello world'" || fail=1

# 5. Go
go run "$TMPDIR/main.go" || fail=1

if [[ $fail -ne 0 ]]; then
  exit 1
fi

exit 0
