#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
local_root="${repo_root}/.local"
downloads="${local_root}/downloads"
toolchains="${local_root}/toolchains"
rustup_home="${local_root}/wsl-rustup"
cargo_home="${local_root}/wsl-cargo"
zig_root="${toolchains}/zig"

mkdir -p "${downloads}" "${toolchains}" "${rustup_home}" "${cargo_home}"

rustup_url="https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"
rustup_bin="${downloads}/rustup-init-x86_64-unknown-linux-gnu"
rustup_checksum="${downloads}/rustup-init-x86_64-unknown-linux-gnu.sha256"

if [[ ! -x "${cargo_home}/bin/cargo" ]]; then
    curl --proto '=https' --tlsv1.2 --fail --location \
        "${rustup_url}" --output "${rustup_bin}"
    curl --proto '=https' --tlsv1.2 --fail --location \
        "${rustup_url}.sha256" --output "${rustup_checksum}"
    expected_rustup="$(cut -d' ' -f1 "${rustup_checksum}")"
    actual_rustup="$(sha256sum "${rustup_bin}" | cut -d' ' -f1)"
    [[ "${expected_rustup}" == "${actual_rustup}" ]]
    chmod +x "${rustup_bin}"
    RUSTUP_HOME="${rustup_home}" CARGO_HOME="${cargo_home}" \
        "${rustup_bin}" -y --no-modify-path --profile minimal --default-toolchain stable
fi

if [[ ! -x "${zig_root}/zig" ]]; then
    zig_metadata="${downloads}/zig-download-index.json"
    curl --proto '=https' --tlsv1.2 --fail --location \
        "https://ziglang.org/download/index.json" --output "${zig_metadata}"
    mapfile -t zig_values < <(
        python3 - "${zig_metadata}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    metadata = json.load(handle)
release = metadata["0.16.0"]["x86_64-linux"]
print(release["tarball"])
print(release["shasum"])
PY
    )
    zig_url="${zig_values[0]}"
    zig_sha256="${zig_values[1]}"
    zig_archive="${downloads}/zig-x86_64-linux-0.16.0.tar.xz"
    curl --proto '=https' --tlsv1.2 --fail --location \
        "${zig_url}" --output "${zig_archive}"
    echo "${zig_sha256}  ${zig_archive}" | sha256sum --check -
    zig_temp="${toolchains}/zig-extract"
    case "${zig_temp}" in
        "${local_root}"/*) ;;
        *) echo "refusing temporary path outside repository: ${zig_temp}" >&2; exit 1 ;;
    esac
    rm -rf "${zig_temp}"
    mkdir -p "${zig_temp}"
    tar -xJf "${zig_archive}" --strip-components=1 -C "${zig_temp}"
    mv "${zig_temp}" "${zig_root}"
fi

RUSTUP_HOME="${rustup_home}" CARGO_HOME="${cargo_home}" \
    "${cargo_home}/bin/rustc" --version
"${zig_root}/zig" version
