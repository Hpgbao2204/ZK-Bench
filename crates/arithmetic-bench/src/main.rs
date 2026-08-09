use ark_bls12_377::Bls12_377;
use ark_bls12_381::Bls12_381;
use ark_bn254::Bn254;
use ark_bw6_761::BW6_761;
use ark_ec::{CurveGroup, VariableBaseMSM, pairing::Pairing};
use ark_ed_on_bls12_381::{EdwardsProjective as JubjubProjective, Fr as JubjubFr};
use ark_ed_on_bn254::{EdwardsProjective as BabyJubjubProjective, Fr as BabyJubjubFr};
use ark_ff::{FftField, Field, UniformRand};
use ark_poly::{EvaluationDomain, Radix2EvaluationDomain};
use ark_std::rand::{SeedableRng, rngs::StdRng};
use rayon::prelude::*;
use rayon::{ThreadPool, ThreadPoolBuilder};
use std::env;
use std::hint::black_box;
use std::io::{self, Write};
use std::time::Instant;

const SIZES: &[usize] = &[2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048];
const PARALLEL_BATCH: usize = 8;

struct CsvWriter<W> {
    output: W,
    threads: usize,
    parallel: bool,
}

impl<W: Write> CsvWriter<W> {
    fn header(&mut self) -> io::Result<()> {
        writeln!(
            self.output,
            "curve,operation,size,repetition,threads,execution_mode,elapsed_ns,operations"
        )
    }

    fn row(
        &mut self,
        curve: &str,
        operation: &str,
        size: usize,
        repetition: usize,
        elapsed_ns: u128,
        operations: usize,
    ) -> io::Result<()> {
        // Exclude sub-nanosecond/zero observations instead of rounding them.
        if elapsed_ns <= 1 {
            return Ok(());
        }
        writeln!(
            self.output,
            "{curve},{operation},{size},{repetition},{},{},{},{}",
            self.threads,
            if self.parallel { "parallel" } else { "serial" },
            elapsed_ns,
            operations,
        )
    }
}

fn field_benchmark<F: Field + UniformRand + Send + Sync>(
    writer: &mut CsvWriter<impl Write>,
    curve: &str,
    repetitions: usize,
    seed: u64,
    pool: &ThreadPool,
    parallel: bool,
) -> io::Result<()> {
    for operation in ["field_add", "field_mul", "field_inv"] {
        for &size in SIZES {
            for repetition in 0..repetitions {
                let mut rng = StdRng::seed_from_u64(
                    seed ^ (curve.len() as u64).wrapping_mul(0x9e37_79b9)
                        ^ (size as u64).rotate_left(17)
                        ^ repetition as u64,
                );
                let mut lanes = (0..PARALLEL_BATCH)
                    .map(|_| {
                        let mut left = F::rand(&mut rng);
                        if left.is_zero() {
                            left = F::ONE;
                        }
                        (left, F::rand(&mut rng))
                    })
                    .collect::<Vec<_>>();
                let start = Instant::now();
                let update = |(left, right): &mut (F, F)| {
                    for _ in 0..size {
                        let value = match operation {
                            "field_add" => left.clone() + right.clone(),
                            "field_mul" => left.clone() * right.clone(),
                            "field_inv" => left.inverse().unwrap_or(F::ONE),
                            _ => unreachable!(),
                        };
                        *left = value;
                    }
                    black_box(left);
                };
                if parallel {
                    pool.install(|| lanes.par_iter_mut().for_each(update));
                } else {
                    lanes.iter_mut().for_each(update);
                }
                writer.row(
                    curve,
                    operation,
                    size,
                    repetition,
                    start.elapsed().as_nanos(),
                    size * PARALLEL_BATCH,
                )?;
            }
        }
    }
    Ok(())
}

fn msm_benchmark<G>(
    writer: &mut CsvWriter<impl Write>,
    curve: &str,
    repetitions: usize,
    seed: u64,
    pool: &ThreadPool,
    parallel: bool,
) -> io::Result<()>
where
    G: CurveGroup + Send + Sync,
    G::Affine: Send + Sync,
    G::ScalarField: Send + Sync,
{
    for &size in SIZES {
        for repetition in 0..repetitions {
            let mut rng = StdRng::seed_from_u64(
                seed ^ 0x4d53_4d00 ^ (curve.len() as u64) ^ size as u64 ^ repetition as u64,
            );
            let batches = (0..PARALLEL_BATCH)
                .map(|_| {
                    let bases = (0..size)
                        .map(|_| G::rand(&mut rng).into_affine())
                        .collect::<Vec<_>>();
                    let scalars = (0..size)
                        .map(|_| G::ScalarField::rand(&mut rng))
                        .collect::<Vec<_>>();
                    (bases, scalars)
                })
                .collect::<Vec<_>>();
            let start = Instant::now();
            let msm = |(bases, scalars): &(Vec<G::Affine>, Vec<G::ScalarField>)| {
                black_box(<G as VariableBaseMSM>::msm_unchecked(bases, scalars));
            };
            if parallel {
                pool.install(|| batches.par_iter().for_each(msm));
            } else {
                batches.iter().for_each(msm);
            }
            writer.row(
                curve,
                "msm",
                size,
                repetition,
                start.elapsed().as_nanos(),
                size * PARALLEL_BATCH,
            )?;
        }
    }
    Ok(())
}

fn ntt_benchmark<F: FftField + UniformRand + Send + Sync>(
    writer: &mut CsvWriter<impl Write>,
    curve: &str,
    repetitions: usize,
    seed: u64,
    pool: &ThreadPool,
    parallel: bool,
) -> io::Result<()> {
    for &size in SIZES {
        let Some(domain) = Radix2EvaluationDomain::<F>::new(size) else {
            continue;
        };
        for repetition in 0..repetitions {
            let mut rng = StdRng::seed_from_u64(
                seed ^ 0x4e54_5400 ^ (curve.len() as u64) ^ size as u64 ^ repetition as u64,
            );
            let mut batches = (0..PARALLEL_BATCH)
                .map(|_| (0..size).map(|_| F::rand(&mut rng)).collect::<Vec<_>>())
                .collect::<Vec<_>>();
            let start = Instant::now();
            let transform = |coefficients: &mut Vec<F>| {
                domain.fft_in_place(coefficients);
                black_box(coefficients);
            };
            if parallel {
                pool.install(|| batches.par_iter_mut().for_each(transform));
            } else {
                batches.iter_mut().for_each(transform);
            }
            writer.row(
                curve,
                "ntt",
                size,
                repetition,
                start.elapsed().as_nanos(),
                PARALLEL_BATCH * size * size.ilog2() as usize,
            )?;
        }
    }
    Ok(())
}

fn pairing_benchmark<P>(
    writer: &mut CsvWriter<impl Write>,
    curve: &str,
    repetitions: usize,
    seed: u64,
    pool: &ThreadPool,
    parallel: bool,
) -> io::Result<()>
where
    P: Pairing + Send + Sync,
    P::G1Affine: Send + Sync,
    P::G2Affine: Send + Sync,
{
    let pairing_sizes: &[usize] = &[2, 4, 8, 16, 32, 64];
    for &size in pairing_sizes {
        for repetition in 0..repetitions {
            let mut rng = StdRng::seed_from_u64(
                seed ^ 0x5041_4952 ^ (curve.len() as u64) ^ size as u64 ^ repetition as u64,
            );
            let batches = (0..PARALLEL_BATCH)
                .map(|_| {
                    let g1 = (0..size)
                        .map(|_| P::G1::rand(&mut rng).into_affine())
                        .collect::<Vec<_>>();
                    let g2 = (0..size)
                        .map(|_| P::G2::rand(&mut rng).into_affine())
                        .collect::<Vec<_>>();
                    (g1, g2)
                })
                .collect::<Vec<_>>();
            let start = Instant::now();
            let pairing = |(g1, g2): &(Vec<P::G1Affine>, Vec<P::G2Affine>)| {
                let _ = black_box(P::multi_pairing(g1.clone(), g2.clone()));
            };
            if parallel {
                pool.install(|| batches.par_iter().for_each(pairing));
            } else {
                batches.iter().for_each(pairing);
            }
            writer.row(
                curve,
                "multi_pairing",
                size,
                repetition,
                start.elapsed().as_nanos(),
                size * PARALLEL_BATCH,
            )?;
        }
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let mut repetitions = 10_usize;
    let mut seed = 2026_u64;
    let mut threads = 1_usize;
    let mut parallel = false;
    let mut output_path = None;
    let mut args = env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--repetitions" => {
                repetitions = args
                    .next()
                    .ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidInput, "missing repetitions")
                    })?
                    .parse()
                    .map_err(|_| {
                        io::Error::new(io::ErrorKind::InvalidInput, "invalid repetitions")
                    })?;
            }
            "--seed" => {
                seed = args
                    .next()
                    .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing seed"))?
                    .parse()
                    .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "invalid seed"))?;
            }
            "--threads" => {
                threads = args
                    .next()
                    .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing threads"))?
                    .parse()
                    .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "invalid threads"))?;
            }
            "--parallel" => parallel = true,
            "--output" => output_path = args.next(),
            other => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("unknown argument {other}"),
                ));
            }
        }
    }
    if repetitions < 2 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "repetitions must exceed the excluded boundary",
        ));
    }
    if threads == 0 || (!parallel && threads != 1) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "serial mode requires --threads 1; parallel mode requires a positive --threads",
        ));
    }
    let pool = ThreadPoolBuilder::new()
        .num_threads(threads)
        .build()
        .map_err(|error| io::Error::other(format!("failed to build thread pool: {error}")))?;

    let file = output_path
        .as_deref()
        .map(std::fs::File::create)
        .transpose()?;
    let writer: Box<dyn Write> = match file {
        Some(file) => Box::new(file),
        None => Box::new(io::stdout()),
    };
    let mut writer = CsvWriter {
        output: writer,
        threads,
        parallel,
    };
    writer.header()?;

    field_benchmark::<ark_bn254::Fr>(&mut writer, "BN254", repetitions, seed, &pool, parallel)?;
    field_benchmark::<ark_bls12_377::Fr>(
        &mut writer,
        "BLS12-377",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    field_benchmark::<ark_bls12_381::Fr>(
        &mut writer,
        "BLS12-381",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    field_benchmark::<ark_bw6_761::Fr>(&mut writer, "BW6-761", repetitions, seed, &pool, parallel)?;
    field_benchmark::<JubjubFr>(
        &mut writer,
        "Jubjub-BLS12-381",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    field_benchmark::<BabyJubjubFr>(
        &mut writer,
        "BabyJubjub-BN254",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;

    ntt_benchmark::<ark_bn254::Fr>(&mut writer, "BN254", repetitions, seed, &pool, parallel)?;
    ntt_benchmark::<ark_bls12_377::Fr>(
        &mut writer,
        "BLS12-377",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    ntt_benchmark::<ark_bls12_381::Fr>(
        &mut writer,
        "BLS12-381",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    ntt_benchmark::<ark_bw6_761::Fr>(&mut writer, "BW6-761", repetitions, seed, &pool, parallel)?;
    ntt_benchmark::<JubjubFr>(
        &mut writer,
        "Jubjub-BLS12-381",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    ntt_benchmark::<BabyJubjubFr>(
        &mut writer,
        "BabyJubjub-BN254",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;

    msm_benchmark::<ark_bn254::G1Projective>(
        &mut writer,
        "BN254-G1",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    msm_benchmark::<ark_bls12_377::G1Projective>(
        &mut writer,
        "BLS12-377-G1",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    msm_benchmark::<ark_bls12_381::G1Projective>(
        &mut writer,
        "BLS12-381-G1",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    msm_benchmark::<ark_bw6_761::G1Projective>(
        &mut writer,
        "BW6-761-G1",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    msm_benchmark::<JubjubProjective>(
        &mut writer,
        "Jubjub-BLS12-381",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;
    msm_benchmark::<BabyJubjubProjective>(
        &mut writer,
        "BabyJubjub-BN254",
        repetitions,
        seed,
        &pool,
        parallel,
    )?;

    pairing_benchmark::<Bn254>(&mut writer, "BN254", repetitions, seed, &pool, parallel)?;
    pairing_benchmark::<Bls12_377>(&mut writer, "BLS12-377", repetitions, seed, &pool, parallel)?;
    pairing_benchmark::<Bls12_381>(&mut writer, "BLS12-381", repetitions, seed, &pool, parallel)?;
    pairing_benchmark::<BW6_761>(&mut writer, "BW6-761", repetitions, seed, &pool, parallel)?;
    Ok(())
}
