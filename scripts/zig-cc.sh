#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Zig 0.16 uses ``x86_64-linux-gnu`` target spelling; cc-rs emits the
# equivalent Rust triple with an ``unknown`` vendor component.
args=()
for arg in "$@"; do
    case "$arg" in
        --target=*-unknown-linux-*) args+=("${arg/unknown-/}") ;;
        -target=*-unknown-linux-*) args+=("${arg/unknown-/}") ;;
        *) args+=("$arg") ;;
    esac
done
exec "${repo_root}/.local/toolchains/zig/zig" cc "${args[@]}"
