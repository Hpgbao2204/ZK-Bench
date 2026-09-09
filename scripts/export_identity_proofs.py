#!/usr/bin/env python3
"""Export one verified identity proof from each paper-scale adapter."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from zkbench.adapter_protocol import AdapterRequest  # noqa: E402
from zkbench.adapter_runner import execute_adapter  # noqa: E402


MATRIX = REPO / "configs" / "reproduction-scale-campaigns.json"
OUTPUT = REPO / ".local" / "proof-artifacts" / "identity"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    workload = matrix["workloads"]["identity"]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "evidence_class": "measured",
        "purpose": "serialized identity proof publication artifacts",
        "adapter_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        "tracked_worktree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=REPO,
                text=True,
            ).strip()
        ),
        "artifacts": {},
    }
    for system, adapter in matrix["adapters"].items():
        relative_artifact = Path(".local") / "proof-artifacts" / "identity" / f"{system}.bin"
        request = AdapterRequest(
            run_id=f"identity-proof-export-{system}",
            workload=workload["workload"],
            scale=workload["scales"][0],
            threads=matrix["defaults"]["threads"][0],
            seed=matrix["defaults"]["seed"],
            mode="cold",
            parameters=dict(workload["parameters"]),
        )
        execution = execute_adapter(
            [str(REPO / adapter["command"][0])],
            request,
            timeout_seconds=matrix["defaults"]["timeout_seconds"],
            sampling_interval_ms=matrix["defaults"]["sampling_interval_ms"],
            environment={
                **os.environ,
                "ZKBENCH_PROOF_ARTIFACT_PATH": relative_artifact.as_posix(),
            },
        )
        if not execution.succeeded or execution.result is None:
            raise RuntimeError(
                f"{system} proof export failed: {execution.protocol_error or execution.stderr}"
            )
        artifact = REPO / relative_artifact
        size = artifact.stat().st_size
        if size != execution.result.proof_bytes:
            raise RuntimeError(
                f"{system} artifact size {size} differs from adapter result "
                f"{execution.result.proof_bytes}"
            )
        manifest["artifacts"][system] = {
            "adapter": execution.result.adapter,
            "binary": adapter["command"][0],
            "binary_sha256": sha256(REPO / adapter["command"][0]),
            "path": relative_artifact.as_posix(),
            "proof_bytes": size,
            "proof_sha256": sha256(artifact),
            "verify_ok": execution.result.verify_ok,
            "native_work_units": execution.result.native_work_units,
            "relation_unit": execution.result.relation_unit,
        }
        print(f"{system}: verified proof_bytes={size}", flush=True)
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"manifest: {OUTPUT / 'manifest.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
