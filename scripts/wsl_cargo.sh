#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
local_root="${repo_root}/.local"

export RUSTUP_HOME="${local_root}/wsl-rustup"
export CARGO_HOME="${local_root}/wsl-cargo"
export CARGO_TARGET_DIR="${local_root}/wsl-cargo-target"
export GIT_CONFIG_GLOBAL="${local_root}/wsl-git-home/gitconfig"
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER="${repo_root}/scripts/zig-cc.sh"
export CC="${repo_root}/scripts/zig-cc.sh"
export CXX="${repo_root}/scripts/zig-cxx.sh"

mkdir -p "${CARGO_HOME}" "${CARGO_TARGET_DIR}" "$(dirname "${GIT_CONFIG_GLOBAL}")"
exec "${CARGO_HOME}/bin/cargo" "$@"
