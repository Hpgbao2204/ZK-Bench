#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Keep the C and C++ wrappers on the same target spelling as Zig 0.16.
args=()
for arg in "$@"; do
    case "$arg" in
        --target=*-unknown-linux-*) args+=("${arg/unknown-/}") ;;
        -target=*-unknown-linux-*) args+=("${arg/unknown-/}") ;;
        *) args+=("$arg") ;;
    esac
done
exec "${repo_root}/.local/toolchains/zig/zig" c++ "${args[@]}"
