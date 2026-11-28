//! Run with `python -m research.bench_shadow_compare <local backend clone>`.

use std::hint::black_box;
use std::time::Instant;

mod baseline {
    include!(env!("SHADOW_REFERENCE"));
}
#[path = "../native/velkin-render/src/shadow_bytes.rs"]
mod current;

type Bounds = fn(&[u8], &[u8]) -> Option<(usize, usize)>;

fn baseline_bounds(a: &[u8], b: &[u8]) -> Option<(usize, usize)> {
    Some((
        baseline::first_difference(a, b)?,
        baseline::last_difference(a, b)?,
    ))
}

fn current_bounds(a: &[u8], b: &[u8]) -> Option<(usize, usize)> {
    Some((
        current::first_difference(a, b)?,
        current::last_difference(a, b)?,
    ))
}

fn main() {
    let methods: [Bounds; 2] = [baseline_bounds, current_bounds];
    println!("width,case,logical_bytes_per_pass,baseline_median_us,current_median_us");
    for (size, width, repeats) in [
        (1usize << 20, 8, 30),
        (1 << 20, 31, 120),
        (64 << 20, 65536, 3),
    ] {
        let bytes: Vec<u8> = (0..size + 21).map(|i| (i * 9) as u8).collect();
        let a = &bytes[11..11 + size];
        let mut other = bytes.clone();
        let b = &mut other[11..11 + size];
        for case in ["unchanged", "interior", "last", "first"] {
            b.copy_from_slice(a);
            for (i, part) in b.chunks_mut(width).enumerate() {
                let changed = match case {
                    "unchanged" => continue,
                    "interior" => (i * 977 + 11) % part.len(),
                    "last" => part.len() - 1,
                    _ => 0,
                };
                part[changed] ^= 7;
            }
            let mut times = [[0; 7]; 2];
            for pass in 0..7 {
                let mut result = [0; 2];
                for off in 0..2 {
                    let m = (pass + off) % 2;
                    let start = Instant::now();
                    for _ in 0..repeats {
                        for (p, q) in a.chunks(width).zip(b.chunks(width)) {
                            if let Some((first, last)) = black_box(methods[m])(p, q) {
                                result[m] += black_box(first + last);
                            }
                        }
                    }
                    times[m][pass] = start.elapsed().as_micros();
                }
                assert_eq!(result[0], result[1]);
            }
            times.iter_mut().for_each(|t| t.sort_unstable());
            println!(
                "{width},{case},{},{},{}",
                size * repeats,
                times[0][3],
                times[1][3]
            );
        }
    }
    // The unchanged path never requests temporary bytes; short successful reads
    // reuse the initialized prefix without clearing its contents between calls.
    let mut scratch = Vec::new();
    assert_eq!(scratch.capacity(), 0);
    current::read_scratch(&mut scratch, 31).fill(42);
    assert_eq!(current::read_scratch(&mut scratch, 8), &[42; 8]);
}
