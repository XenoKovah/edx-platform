//! Pure, safe computation: replays learners' scores into their course grade at each point in time.
#![forbid(unsafe_code)]

pub const MAX_USERS: usize = 1000;
pub const MAX_POINTS: usize = 1000;
pub const MAX_TYPES: usize = 64;
pub const MAX_SUBS: usize = 20_000;
pub const MAX_INPUT_F64: usize = 16 * 1024 * 1024; // 128 MiB of f64; far above any real course

pub const MAGIC: f64 = 5_067_591.0; // "PTL" tag
pub const VERSION: f64 = 1.0;

pub const E_HEADER: i32 = -1;
pub const E_LIMIT: i32 = -2;
pub const E_LENGTH: i32 = -3;
pub const E_VALUE: i32 = -4;
pub const E_INDEX: i32 = -5;
pub const E_ORDER: i32 = -6;

/// Reads a whole number in [0, max] from an f64 slot.
fn count(v: f64, max: usize) -> Result<usize, i32> {
    if !v.is_finite() || v < 0.0 || v.fract() != 0.0 || v > max as f64 {
        return Err(E_LIMIT);
    }
    Ok(v as usize)
}

fn finite(v: f64) -> Result<f64, i32> {
    if v.is_finite() { Ok(v) } else { Err(E_VALUE) }
}

struct Cursor<'a> {
    data: &'a [f64],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [f64], i32> {
        let end = self.pos.checked_add(n).ok_or(E_LENGTH)?;
        let out = self.data.get(self.pos..end).ok_or(E_LENGTH)?;
        self.pos = end;
        Ok(out)
    }
}

struct GradeType {
    min_count: usize,
    drop_count: usize,
    weight: f64,
    subs: Vec<usize>,
}

/// numpy.around(x, 4) as used by edx-platform scores.compute_percent.
fn round4(x: f64) -> f64 {
    (x * 10000.0).round_ties_even() / 10000.0
}

/// edx-platform CourseGrade._compute_percent, returned as a whole percent.
fn course_percent(types: &[GradeType], possible: &[f64], earned: &[f64], scratch: &mut Vec<f64>) -> f32 {
    let mut total = 0.0f64;
    for t in types {
        scratch.clear();
        for &i in &t.subs {
            let pos = possible[i];
            scratch.push(if pos > 0.0 { round4(earned[i] / pos) } else { 0.0 });
        }
        let n = t.min_count.max(scratch.len());
        scratch.resize(n, 0.0);
        let mut sum = 0.0;
        let denom;
        if t.drop_count > 0 {
            scratch.sort_by(|a, b| b.total_cmp(a));
            let keep = n.saturating_sub(t.drop_count);
            for v in scratch.iter().take(keep) {
                sum += *v;
            }
            denom = keep;
        } else {
            for v in scratch.iter() {
                sum += *v;
            }
            denom = n;
        }
        let pct = if denom > 0 { sum / denom as f64 } else { 0.0 };
        total += pct * t.weight;
    }
    // round_away_from_zero(total * 100 + 0.05); total is never negative.
    ((total * 100.0 + 0.05) + 0.5).floor() as f32
}

/// Input layout (all f64):
///   MAGIC, VERSION, n_types, n_subs, n_users, n_points
///   n_types x [min_count, drop_count, weight]
///   n_subs  x [type_index, possible]
///   n_points x [boundary_seconds]           (ascending)
///   n_users x [n_base, n_events]
///   for each user: n_base x [sub, earned], then n_events x [t, sub, earned] (t ascending)
/// Output: n_users * n_points whole percents, user-major.
pub fn compute(input: &[f64], out: &mut Vec<f32>) -> Result<(u32, u32), i32> {
    let mut c = Cursor { data: input, pos: 0 };
    let head = c.take(6).map_err(|_| E_HEADER)?;
    if head[0] != MAGIC || head[1] != VERSION {
        return Err(E_HEADER);
    }
    let n_types = count(head[2], MAX_TYPES)?;
    let n_subs = count(head[3], MAX_SUBS)?;
    let n_users = count(head[4], MAX_USERS)?;
    let n_points = count(head[5], MAX_POINTS)?;

    let mut types: Vec<GradeType> = Vec::with_capacity(n_types);
    for _ in 0..n_types {
        let r = c.take(3)?;
        let weight = finite(r[2])?;
        if !(0.0..=1.0).contains(&weight) {
            return Err(E_VALUE);
        }
        types.push(GradeType {
            min_count: count(r[0], MAX_SUBS)?,
            drop_count: count(r[1], MAX_SUBS)?,
            weight,
            subs: Vec::new(),
        });
    }
    let mut possible = vec![0.0f64; n_subs];
    for (i, slot) in possible.iter_mut().enumerate() {
        let r = c.take(2)?;
        let t = count(r[0], n_types.saturating_sub(1))?;
        if n_types == 0 {
            return Err(E_INDEX);
        }
        let p = finite(r[1])?;
        if p <= 0.0 {
            return Err(E_VALUE);
        }
        *slot = p;
        types[t].subs.push(i);
    }
    let bounds = c.take(n_points)?;
    for w in bounds.windows(2) {
        if !(w[0] < w[1]) {
            return Err(E_ORDER);
        }
    }
    for &b in bounds {
        finite(b)?;
    }
    let counts = c.take(n_users.checked_mul(2).ok_or(E_LENGTH)?)?;

    let total = n_users.checked_mul(n_points).ok_or(E_LENGTH)?;
    out.clear();
    out.resize(total, 0.0);
    let mut earned = vec![0.0f64; n_subs];
    let mut scratch: Vec<f64> = Vec::new();

    for u in 0..n_users {
        let n_base = count(counts[2 * u], MAX_SUBS)?;
        let n_events = count(counts[2 * u + 1], MAX_INPUT_F64)?;
        earned.iter_mut().for_each(|e| *e = 0.0);
        for r in c.take(n_base.checked_mul(2).ok_or(E_LENGTH)?)?.chunks_exact(2) {
            let s = count(r[0], n_subs.saturating_sub(1)).map_err(|_| E_INDEX)?;
            *earned.get_mut(s).ok_or(E_INDEX)? += finite(r[1])?;
        }
        let ev = c.take(n_events.checked_mul(3).ok_or(E_LENGTH)?)?;
        let row = &mut out[u * n_points..(u + 1) * n_points];
        let mut e = 0usize;
        let mut last_t = f64::NEG_INFINITY;
        let mut current = course_percent(&types, &possible, &earned, &mut scratch);
        for (k, &b) in bounds.iter().enumerate() {
            let mut changed = false;
            while let Some(r) = ev.get(e * 3..e * 3 + 3) {
                let t = finite(r[0])?;
                if t < last_t {
                    return Err(E_ORDER);
                }
                if t > b {
                    break;
                }
                last_t = t;
                let s = count(r[1], n_subs.saturating_sub(1)).map_err(|_| E_INDEX)?;
                *earned.get_mut(s).ok_or(E_INDEX)? += finite(r[2])?;
                changed = true;
                e += 1;
            }
            if changed {
                current = course_percent(&types, &possible, &earned, &mut scratch);
            }
            row[k] = current;
        }
    }
    if c.pos != input.len() {
        return Err(E_LENGTH);
    }
    Ok((n_users as u32, n_points as u32))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(types: &[[f64; 3]], subs: &[[f64; 2]], bounds: &[f64], users: &[(Vec<[f64; 2]>, Vec<[f64; 3]>)]) -> Vec<f64> {
        let mut v = vec![MAGIC, VERSION, types.len() as f64, subs.len() as f64, users.len() as f64, bounds.len() as f64];
        types.iter().for_each(|t| v.extend_from_slice(t));
        subs.iter().for_each(|s| v.extend_from_slice(s));
        v.extend_from_slice(bounds);
        users.iter().for_each(|(b, e)| v.extend_from_slice(&[b.len() as f64, e.len() as f64]));
        for (b, e) in users {
            b.iter().for_each(|x| v.extend_from_slice(x));
            e.iter().for_each(|x| v.extend_from_slice(x));
        }
        v
    }

    #[test]
    fn replay_steps_up() {
        // One type, min_count 4, four subsections of 1 point each.
        let input = msg(&[[4.0, 0.0, 1.0]], &[[0.0, 1.0]; 4], &[0.0, 10.0, 20.0],
                        &[(vec![[0.0, 1.0]], vec![[5.0, 1.0, 1.0], [15.0, 2.0, 1.0], [16.0, 3.0, 1.0]])]);
        let mut out = Vec::new();
        assert_eq!(compute(&input, &mut out), Ok((1, 3)));
        assert_eq!(out, vec![25.0, 50.0, 100.0]);
    }

    #[test]
    fn drops_lowest() {
        let input = msg(&[[0.0, 1.0, 1.0]], &[[0.0, 1.0]; 2], &[0.0], &[(vec![[0.0, 1.0]], vec![])]);
        let mut out = Vec::new();
        assert_eq!(compute(&input, &mut out), Ok((1, 1)));
        assert_eq!(out, vec![100.0]);
    }

    #[test]
    fn rejects_bad_input_without_panicking() {
        let good = msg(&[[1.0, 0.0, 1.0]], &[[0.0, 1.0]], &[0.0, 1.0], &[(vec![], vec![[0.5, 0.0, 1.0]])]);
        let mut out = Vec::new();
        assert!(compute(&good, &mut out).is_ok());
        // Truncated at every possible length.
        for n in 0..good.len() {
            assert!(compute(&good[..n], &mut out).is_err(), "len {n}");
        }
        // Extra trailing data.
        let mut long = good.clone();
        long.push(0.0);
        assert_eq!(compute(&long, &mut out), Err(E_LENGTH));
        // Sub index out of range, NaN, negative/huge counts, unsorted bounds.
        let mut bad = good.clone();
        let last = bad.len() - 2;
        bad[last] = 7.0;
        assert_eq!(compute(&bad, &mut out), Err(E_INDEX));
        let mut nan = good.clone();
        let l = nan.len() - 1;
        nan[l] = f64::NAN;
        assert_eq!(compute(&nan, &mut out), Err(E_VALUE));
        for (slot, v) in [(4, 1001.0), (4, -1.0), (5, 1e18), (3, 0.5)] {
            let mut b = good.clone();
            b[slot] = v;
            assert!(compute(&b, &mut out).is_err());
        }
        let unsorted = msg(&[[1.0, 0.0, 1.0]], &[[0.0, 1.0]], &[1.0, 0.0], &[]);
        assert_eq!(compute(&unsorted, &mut out), Err(E_ORDER));
    }
}
