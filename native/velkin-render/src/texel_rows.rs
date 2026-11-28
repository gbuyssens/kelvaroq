//! Contiguous copies shared by every row and array slice of one tiled mip.

#[derive(Default)]
pub struct RowPlan {
    runs: Box<[(usize, usize, usize)]>,
    body_width: usize,
}

impl RowPlan {
    /// Group consecutive element addresses. The caller supplies the byte offset
    /// of each column relative to the tile row in increasing logical x order.
    pub fn new(width: u32, element_bytes: usize, mut column: impl FnMut(u32) -> usize) -> Self {
        if width == 0 {
            return Self::default();
        }
        let mut runs = Vec::new();
        let mut first_tiled = column(0);
        let mut first_linear = 0;
        for x in 1..width {
            let tiled = column(x);
            let linear = x as usize * element_bytes;
            if first_tiled.checked_add(linear - first_linear) != Some(tiled) {
                runs.push((first_tiled, first_linear, linear - first_linear));
                first_tiled = tiled;
                first_linear = linear;
            }
        }
        // A partial last block can shorten the final run.
        let body_width = runs.first().map_or(0, |run| run.2);
        let body_width = if runs.iter().all(|run| run.2 == body_width) {
            body_width
        } else {
            0
        };
        runs.push((
            first_tiled,
            first_linear,
            width as usize * element_bytes - first_linear,
        ));
        Self {
            runs: runs.into_boxed_slice(),
            body_width,
        }
    }

    /// Visit each copy as (tiled byte offset, linear byte offset, byte count).
    #[inline]
    pub fn copy_row(&self, tiled: usize, linear: usize, mut copy: impl FnMut(usize, usize, usize)) {
        let Some((&(last_t, last_l, last_n), body)) = self.runs.split_last() else {
            return;
        };
        #[inline]
        fn body_runs<const N: usize>(
            runs: &[(usize, usize, usize)],
            t: usize,
            l: usize,
            copy: &mut impl FnMut(usize, usize, usize),
        ) {
            for &(tiled, linear, length) in runs {
                copy(t + tiled, l + linear, if N == 0 { length } else { N });
            }
        }
        match self.body_width {
            8 => body_runs::<8>(body, tiled, linear, &mut copy),
            16 => body_runs::<16>(body, tiled, linear, &mut copy),
            32 => body_runs::<32>(body, tiled, linear, &mut copy),
            _ => body_runs::<0>(body, tiled, linear, &mut copy),
        }
        copy(tiled + last_t, linear + last_l, last_n);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_recomputation_when_rows_reuse_the_plan() {
        for columns in [
            &[2, 4, 6, 8, 24, 26, 28, 30, 20][..],
            &[30, 32, 36, 8, 10, 12, 14],
            &[],
            &[6],
        ] {
            let mut calls = 0;
            let plan = RowPlan::new(columns.len() as u32, 2, |x| {
                calls += 1;
                columns[x as usize]
            });
            for row in [0, 100, 200] {
                let mut actual = Vec::new();
                plan.copy_row(row, row + 50, |t, l, n| {
                    for i in 0..n {
                        actual.push((t + i, l + i));
                    }
                });
                let expected: Vec<_> = columns
                    .iter()
                    .enumerate()
                    .flat_map(|(x, &t)| (0..2).map(move |i| (row + t + i, row + 50 + x * 2 + i)))
                    .collect();
                assert_eq!(actual, expected);
            }
            assert_eq!(calls, columns.len());
        }
    }
}
