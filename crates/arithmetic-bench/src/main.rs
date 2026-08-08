use ark_bls12_381::Bls12_381;
use ark_bn254::Bn254;
use ark_ec::{AffineRepr, CurveGroup, Pairing, VariableBaseMSM};
use ark_ed_on_bls12_381::{EdwardsProjective as JubjubProjective, Fr as JubjubFr};
use ark_ed_on_bn254::{EdwardsProjective as BabyJubjubProjective, Fr as BabyJubjubFr};
use ark_ff::{Field, UniformRand};
use ark_std::rand::{SeedableRng, rngs::StdRng};
use std::env;
use std::hint::black_box;
use std::io::{self, Write};
use std::time::Instant;

const SIZES: &[usize] = &[2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048];

struct CsvWriter<W> {
    output: W,
}

impl<W: Write> CsvWriter<W> {
    fn header(&mut self) -> io::Result<()> {
        writeln!(
            self.output,
            "curve,operation,size,repetition,elapsed_ns,operations"
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
            "{curve},{operation},{size},{repetition},{},{}",
            elapsed_ns, operations
        )
    }
}

fn field_benchmark<F: Field + UniformRand>(
    writer: &mut CsvWriter<impl Write>,
    curve: &str,
    repetitions: usize,
    seed: u64,
) -> io::Result<()> {
    for operation in ["field_add", "field_mul", "field_inv"] {
        for &size in SIZES {
            for repetition in 0..repetitions {
                let mut rng = StdRng::seed_from_u64(
                    seed ^ (curve.len() as u64).wrapping_mul(0x9e37_79b9)
                        ^ (size as u64).rotate_left(17)
                        ^ repetition as u64,
                );
                let mut left = F::rand(&mut rng);
                if left.is_zero() {
                    left = F::ONE;
                }
                let right = F::rand(&mut rng);
                let start = Instant::now();
                for _ in 0..size {
                    left = match operation {
                        "field_add" => left + right,
                        "field_mul" => left * right,
                        "field_inv" => left.inverse().unwrap_or(F::ONE),
                        _ => unreachable!(),
                    };
                    black_box(&left);
                }
                writer.row(
                    curve,
                    operation,
                    size,
                    repetition,
                    start.elapsed().as_nanos(),
                    size,
                )?;
            }
        }
    }
    Ok(())
}

fn msm_benchmark<G: CurveGroup>(
    writer: &mut CsvWriter<impl Write>,
    curve: &str,
    repetitions: usize,
    seed: u64,
) -> io::Result<()> {
    for &size in SIZES {
        for repetition in 0..repetitions {
            let mut rng = StdRng::seed_from_u64(
                seed ^ 0x4d53_4d00 ^ (curve.len() as u64) ^ size as u64 ^ repetition as u64,
            );
            let bases = (0..size)
                .map(|_| G::rand(&mut rng).into_affine())
                .collect::<Vec<_>>();
            let scalars = (0..size)
                .map(|_| G::ScalarField::rand(&mut rng))
                .collect::<Vec<_>>();
            let start = Instant::now();
            let result = <G as VariableBaseMSM>::msm_unchecked(&bases, &scalars);
            black_box(result);
            writer.row(
                curve,
                "msm",
                size,
                repetition,
                start.elapsed().as_nanos(),
                size,
            )?;
        }
    }
    Ok(())
}

fn pairing_benchmark<P: Pairing>(
    writer: &mut CsvWriter<impl Write>,
    curve: &str,
    repetitions: usize,
    seed: u64,
) -> io::Result<()> {
    let pairing_sizes: &[usize] = &[2, 4, 8, 16, 32, 64];
    for &size in pairing_sizes {
        for repetition in 0..repetitions {
            let mut rng = StdRng::seed_from_u64(
                seed ^ 0x5041_4952 ^ (curve.len() as u64) ^ size as u64 ^ repetition as u64,
            );
            let g1 = (0..size)
                .map(|_| P::G1::rand(&mut rng).into_affine())
                .collect::<Vec<_>>();
            let g2 = (0..size)
                .map(|_| P::G2::rand(&mut rng).into_affine())
                .collect::<Vec<_>>();
            let start = Instant::now();
            let result = P::multi_pairing(g1, g2);
            black_box(result);
            writer.row(
                curve,
                "multi_pairing",
                size,
                repetition,
                start.elapsed().as_nanos(),
                size,
            )?;
        }
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let mut repetitions = 10_usize;
    let mut seed = 2026_u64;
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

    let file = output_path
        .as_deref()
        .map(std::fs::File::create)
        .transpose()?;
    let writer: Box<dyn Write> = match file {
        Some(file) => Box::new(file),
        None => Box::new(io::stdout()),
    };
    let mut writer = CsvWriter { output: writer };
    writer.header()?;

    field_benchmark::<ark_bn254::Fr>(&mut writer, "BN254", repetitions, seed)?;
    field_benchmark::<ark_bls12_381::Fr>(&mut writer, "BLS12-381", repetitions, seed)?;
    field_benchmark::<JubjubFr>(&mut writer, "Jubjub-BLS12-381", repetitions, seed)?;
    field_benchmark::<BabyJubjubFr>(&mut writer, "BabyJubjub-BN254", repetitions, seed)?;

    msm_benchmark::<ark_bn254::G1Projective>(&mut writer, "BN254-G1", repetitions, seed)?;
    msm_benchmark::<ark_bls12_381::G1Projective>(&mut writer, "BLS12-381-G1", repetitions, seed)?;
    msm_benchmark::<JubjubProjective>(&mut writer, "Jubjub-BLS12-381", repetitions, seed)?;
    msm_benchmark::<BabyJubjubProjective>(&mut writer, "BabyJubjub-BN254", repetitions, seed)?;

    pairing_benchmark::<Bn254>(&mut writer, "BN254", repetitions, seed)?;
    pairing_benchmark::<Bls12_381>(&mut writer, "BLS12-381", repetitions, seed)?;
    Ok(())
}
