# ZK-Bench checkpoint

Đây là checkpoint để tiếp tục ở chat mới. Dự án này độc lập với TrustCircuit.
Mọi thứ phải nằm trong `D:\ZK Bench`; không cài global và phải hỏi trước khi
tải thêm toolchain/dependency. Không compile LaTeX local.

## Đã có

- Runner/adapters cho Groth16, Jellyfish PLONK, Winterfell STARK và
  Bulletproofs range.
- Sáu final application bundles (Groth16/PLONK: credential, state update,
  private swap), bảy cross-family pilot bundles, và arithmetic evidence gồm
  11,240 observations.
- Raw/summary CSV, config hash, binary metadata, valid/invalid semantics,
  native relation units, CPU/RSS/page-fault fields khi available, và serial /
  parallel measurements.
- `python -m unittest discover -s tests -q`: 55 tests passed.
- Public `results/` đã được commit; Paper, scripts vẽ hình và figure assets vẫn
  private/ignored.

## Figure hiện tại

`Paper/figures/final/` chỉ có 24 PDF (`fig01a`--`fig06d`), không có preview,
PNG hay version phụ. Script tạo hình là:

```powershell
python Paper\plot_submission_figures.py --results-root results --output Paper\figures\final
```

Không dùng heatmap, bubble hoặc violin. Các cụm hình lần lượt trả lời:
cross-family prove/verify/proof-size/native-size; application metrics; thread
and resource behavior; private-swap ablation; arithmetic primitives; và
cross-family phase/resource trade-offs.

## Evidence boundary

Đã đo: Groth16 (BN254, BLS12-377, BLS12-381, SHA-256 specialized), PLONK
BN254, Winterfell STARK F128, và Bulletproofs range. Halo2, Gnark,
Circom/SnarkJS, RapidSnark, Bellman, Plonky2/3 chưa có adapter/evidence; không
được vẽ như đã benchmark. GPU cũng chưa available trong WSL2.

`2023-1503.pdf` ở root là bài zk-Bench tham khảo. Bài đó rộng hơn hiện tại:
9 arithmetic libraries, 13 curves, 5 ZKP tools, Exponentiate/SHA-256,
multiple hardware setups và runtime estimator. Project hiện tại sâu hơn ở
semantic fixtures, invalid proofs, native units, phase evidence, parallelism
và application ablation, nhưng chưa vượt bài đó về breadth.

## Hướng nghiên cứu đã thống nhất

Contribution chính không phải “đo nhiều metric”, mà là một reusable evidence
and prediction layer cho ZKP benchmarking:

1. canonical Exponentiate/SHA-256 track trên nhiều family;
2. application track cho credential/state/private-swap;
3. phase/resource/parallel regime analysis;
4. estimator dùng arithmetic + native relation + phase features, kiểm tra bằng
   holdout scales;
5. raw corpus và protocol đo để công trình sau có thể tái sử dụng.

Mỗi figure phải trả lời một research question hoặc chứng minh một claim. Không
được thêm scheme/circuit nếu chưa có adapter, correctness test và result bundle.

## Bước tiếp theo

1. Chốt và xin phép dependency cần cho Halo2/Gnark/Circom/Bellman nếu muốn mở
   rộng family; mọi cài đặt phải local.
2. Implement canonical circuits và chạy correctness trước benchmark.
3. Chạy campaign đa family, setup/prove/verify/proof bytes/native size/CPU/RAM
   và parallelism.
4. Xây estimator + holdout validation và bảng sai số.
5. Chỉ sau khi evidence freeze mới sửa `Paper/main.tex`, `clean-main.tex`,
   response và cover letter.

Paper-facing output chỉ giữ PDF và table fragments; không commit paper source,
review, plotting code hoặc private credentials lên public repository.
