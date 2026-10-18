//! Pure, safe computation: turns learners' recorded subsection grades into their course grade at each point in time.
#![forbid(unsafe_code)]

pub const MAX_USERS: usize = 1000;
pub const MAX_POINTS: usize = 1000;
pub const MAX_TYPES: usize = 64;
pub const MAX_SUBS: usize = 20_000;
pub const MAX_VERSIONS: usize = 100_000;
pub const MAX_INPUT_F64: usize = 16 * 1024 * 1024; // 128 MiB of f64; far above any real course

pub const MAGIC: f64 = 5_067_591.0; // "PTL" tag
pub const VERSION: f64 = 2.0;

/// The sub of an event that records nothing (the course grade only moves onto another version).
pub const NO_SUB: f64 = -1.0;
/// The sub of an event that holds the course grade at a stored value (its earned slot) until the next event.
pub const PIN: f64 = -2.0;

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

/// Reads an index in [0, n) from an f64 slot.
fn index(v: f64, n: usize) -> Result<usize, i32> {
    if n == 0 {
        return Err(E_INDEX);
    }
    count(v, n - 1).map_err(|_| E_INDEX)
}

fn finite(v: f64) -> Result<f64, i32> {
    if v.is_finite() { Ok(v) } else { Err(E_VALUE) }
}

/// A whole percent in [0, 100].
fn whole_percent(v: f64) -> Result<f32, i32> {
    if !v.is_finite() || v < 0.0 || v > 100.0 || v.fract() != 0.0 {
        return Err(E_VALUE);
    }
    Ok(v as f32)
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
}

/// One version of the course: for each assignment type, its subsections on the grade sheet and what each is out of.
struct Version {
    by_type: Vec<Vec<(usize, f64)>>,
}

/// A learner's recorded subsection grades.
struct Records {
    earned: Vec<f64>,
    possible: Vec<f64>,
    has: Vec<bool>,
    touched: Vec<usize>,
}

impl Records {
    fn new(n: usize) -> Self {
        Records { earned: vec![0.0; n], possible: vec![0.0; n], has: vec![false; n], touched: Vec::new() }
    }

    fn clear(&mut self) {
        for &s in &self.touched {
            self.has[s] = false;
        }
        self.touched.clear();
    }

    fn set(&mut self, s: usize, earned: f64, possible: f64) {
        if !self.has[s] {
            self.has[s] = true;
            self.touched.push(s);
        }
        self.earned[s] = earned;
        self.possible[s] = possible;
    }
}

/// numpy.around(x, 4) as used by edx-platform scores.compute_percent.
fn round4(x: f64) -> f64 {
    (x * 10000.0).round_ties_even() / 10000.0
}

/// edx-platform CourseGrade._compute_percent against one version, returned as a whole percent.  A subsection with a
/// recorded grade counts with it (when it is out of more than nothing); one without counts as nothing earned out of
/// its possible in the version.
fn course_percent(types: &[GradeType], version: &Version, rec: &Records, scratch: &mut Vec<f64>) -> f32 {
    let mut total = 0.0f64;
    for (t, gt) in types.iter().enumerate() {
        scratch.clear();
        for &(s, possible) in &version.by_type[t] {
            if rec.has[s] {
                if rec.possible[s] > 0.0 {
                    scratch.push(round4(rec.earned[s] / rec.possible[s]));
                }
            } else if possible > 0.0 {
                scratch.push(0.0);
            }
        }
        let n = gt.min_count.max(scratch.len());
        scratch.resize(n, 0.0);
        let mut sum = 0.0;
        let denom;
        if gt.drop_count > 0 {
            scratch.sort_by(|a, b| b.total_cmp(a));
            let keep = n.saturating_sub(gt.drop_count);
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
        total += pct * gt.weight;
    }
    // round_away_from_zero(total * 100 + 0.05); total is never negative.
    ((total * 100.0 + 0.05) + 0.5).floor() as f32
}

/// Input layout (all f64):
///   MAGIC, VERSION, n_types, n_subs, n_versions, n_users, n_points
///   n_types x [min_count, drop_count, weight]
///   n_versions x [n_entries], then for each version n_entries x [sub, type, possible]
///   n_points x [boundary_seconds]           (ascending)
///   n_users x [version, pin, n_base, n_events]   (the state at the first point; pin -1 = none, else a whole percent)
///   for each user: n_base x [sub, earned, possible], then n_events x [t, version, sub, earned, possible]
///                  (t ascending; sub -1 = nothing recorded, -2 = hold the course grade at `earned`, a whole percent)
/// Output: n_users * n_points whole percents, user-major.
pub fn compute(input: &[f64], out: &mut Vec<f32>) -> Result<(u32, u32), i32> {
    let mut c = Cursor { data: input, pos: 0 };
    let head = c.take(7).map_err(|_| E_HEADER)?;
    if head[0] != MAGIC || head[1] != VERSION {
        return Err(E_HEADER);
    }
    let n_types = count(head[2], MAX_TYPES)?;
    let n_subs = count(head[3], MAX_SUBS)?;
    let n_versions = count(head[4], MAX_VERSIONS)?;
    let n_users = count(head[5], MAX_USERS)?;
    let n_points = count(head[6], MAX_POINTS)?;

    let mut types: Vec<GradeType> = Vec::with_capacity(n_types);
    for _ in 0..n_types {
        let r = c.take(3)?;
        let weight = finite(r[2])?;
        if !(0.0..=1.0).contains(&weight) {
            return Err(E_VALUE);
        }
        types.push(GradeType { min_count: count(r[0], MAX_SUBS)?, drop_count: count(r[1], MAX_SUBS)?, weight });
    }
    let entry_counts = c.take(n_versions)?;
    let mut versions: Vec<Version> = Vec::with_capacity(n_versions);
    for &n in entry_counts {
        let n_entries = count(n, MAX_SUBS)?;
        let mut version = Version { by_type: vec![Vec::new(); n_types] };
        for r in c.take(n_entries.checked_mul(3).ok_or(E_LENGTH)?)?.chunks_exact(3) {
            let s = index(r[0], n_subs)?;
            let t = index(r[1], n_types)?;
            let possible = finite(r[2])?;
            if possible < 0.0 {
                return Err(E_VALUE);
            }
            version.by_type[t].push((s, possible));
        }
        versions.push(version);
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
    let heads = c.take(n_users.checked_mul(4).ok_or(E_LENGTH)?)?;

    let total = n_users.checked_mul(n_points).ok_or(E_LENGTH)?;
    out.clear();
    out.resize(total, 0.0);
    let mut rec = Records::new(n_subs);
    let mut scratch: Vec<f64> = Vec::new();

    for u in 0..n_users {
        let h = &heads[4 * u..4 * u + 4];
        let mut version = index(h[0], n_versions)?;
        let mut pin = if h[1] == NO_SUB { None } else { Some(whole_percent(h[1])?) };
        let n_base = count(h[2], MAX_SUBS)?;
        let n_events = count(h[3], MAX_INPUT_F64)?;
        rec.clear();
        for r in c.take(n_base.checked_mul(3).ok_or(E_LENGTH)?)?.chunks_exact(3) {
            let s = index(r[0], n_subs)?;
            rec.set(s, finite(r[1])?, finite(r[2])?);
        }
        let ev = c.take(n_events.checked_mul(5).ok_or(E_LENGTH)?)?;
        let row = &mut out[u * n_points..(u + 1) * n_points];
        let mut e = 0usize;
        let mut last_t = f64::NEG_INFINITY;
        let mut current = match pin {
            Some(p) => p,
            None => course_percent(&types, &versions[version], &rec, &mut scratch),
        };
        for (k, &b) in bounds.iter().enumerate() {
            let mut changed = false;
            while let Some(r) = ev.get(e * 5..e * 5 + 5) {
                let t = finite(r[0])?;
                if t < last_t {
                    return Err(E_ORDER);
                }
                if t > b {
                    break;
                }
                last_t = t;
                version = index(r[1], n_versions)?;
                if r[2] == PIN {
                    pin = Some(whole_percent(r[3])?);
                } else {
                    pin = None;
                    if r[2] != NO_SUB {
                        let s = index(r[2], n_subs)?;
                        rec.set(s, finite(r[3])?, finite(r[4])?);
                    }
                }
                changed = true;
                e += 1;
            }
            if changed {
                current = match pin {
                    Some(p) => p,
                    None => course_percent(&types, &versions[version], &rec, &mut scratch),
                };
            }
            row[k] = current;
        }
        if e * 5 != ev.len() {
            // Events after the last point: still validated, never shown.
            for r in ev[e * 5..].chunks_exact(5) {
                let t = finite(r[0])?;
                if t < last_t {
                    return Err(E_ORDER);
                }
                last_t = t;
                index(r[1], n_versions)?;
            }
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

    type Entry = [f64; 3];
    type User = ([f64; 2], Vec<[f64; 3]>, Vec<[f64; 5]>);

    fn msg(types: &[[f64; 3]], versions: &[Vec<Entry>], n_subs: usize, bounds: &[f64], users: &[User]) -> Vec<f64> {
        let mut v = vec![MAGIC, VERSION, types.len() as f64, n_subs as f64, versions.len() as f64,
                         users.len() as f64, bounds.len() as f64];
        types.iter().for_each(|t| v.extend_from_slice(t));
        versions.iter().for_each(|ver| v.push(ver.len() as f64));
        versions.iter().for_each(|ver| ver.iter().for_each(|e| v.extend_from_slice(e)));
        v.extend_from_slice(bounds);
        users.iter().for_each(|(h, b, e)| v.extend_from_slice(&[h[0], h[1], b.len() as f64, e.len() as f64]));
        for (_, b, e) in users {
            b.iter().for_each(|x| v.extend_from_slice(x));
            e.iter().for_each(|x| v.extend_from_slice(x));
        }
        v
    }

    fn one_version(n: usize) -> Vec<Vec<Entry>> {
        vec![(0..n).map(|s| [s as f64, 0.0, 1.0]).collect()]
    }

    #[test]
    fn replay_steps_up() {
        // One type, four subsections of 1 point each.
        let input = msg(&[[4.0, 0.0, 1.0]], &one_version(4), 4, &[0.0, 10.0, 20.0],
                        &[([0.0, -1.0], vec![[0.0, 1.0, 1.0]],
                           vec![[5.0, 0.0, 1.0, 1.0, 1.0], [15.0, 0.0, 2.0, 1.0, 1.0], [16.0, 0.0, 3.0, 1.0, 1.0]])]);
        let mut out = Vec::new();
        assert_eq!(compute(&input, &mut out), Ok((1, 3)));
        assert_eq!(out, vec![25.0, 50.0, 100.0]);
    }

    #[test]
    fn drops_lowest() {
        let input = msg(&[[0.0, 1.0, 1.0]], &one_version(2), 2, &[0.0], &[([0.0, -1.0], vec![[0.0, 1.0, 1.0]], vec![])]);
        let mut out = Vec::new();
        assert_eq!(compute(&input, &mut out), Ok((1, 1)));
        assert_eq!(out, vec![100.0]);
    }

    #[test]
    fn a_grade_recorded_against_an_older_version_stands() {
        // Version 0 has subsections 0 and 1; version 1 adds subsection 2 and makes subsection 1 out of 2 points.
        let versions = vec![
            vec![[0.0, 0.0, 1.0], [1.0, 0.0, 1.0]],
            vec![[0.0, 0.0, 1.0], [1.0, 0.0, 2.0], [2.0, 0.0, 1.0]],
        ];
        let user = ([0.0, -1.0], vec![[0.0, 1.0, 1.0], [1.0, 1.0, 1.0]],
                    vec![[10.0, 1.0, -1.0, 0.0, 0.0], [20.0, 1.0, 2.0, 1.0, 1.0]]);
        let input = msg(&[[0.0, 0.0, 1.0]], &versions, 3, &[0.0, 10.0, 20.0], &[user]);
        let mut out = Vec::new();
        assert_eq!(compute(&input, &mut out), Ok((1, 3)));
        // Both stored grades are full marks; then the new subsection counts as nothing; then it is done too.
        assert_eq!(out, vec![100.0, 67.0, 100.0]);
    }

    #[test]
    fn a_subsection_out_of_nothing_is_left_off() {
        let versions = vec![vec![[0.0, 0.0, 1.0], [1.0, 0.0, 0.0]]];
        let input = msg(&[[0.0, 0.0, 1.0]], &versions, 2, &[0.0], &[([0.0, -1.0], vec![[0.0, 1.0, 1.0]], vec![])]);
        let mut out = Vec::new();
        assert_eq!(compute(&input, &mut out), Ok((1, 1)));
        assert_eq!(out, vec![100.0]);
    }

    #[test]
    fn a_pin_holds_until_the_next_event() {
        let user = ([0.0, 98.0], vec![[0.0, 1.0, 1.0]],
                    vec![[5.0, 0.0, -2.0, 40.0, 0.0], [15.0, 0.0, 1.0, 1.0, 1.0]]);
        let input = msg(&[[0.0, 0.0, 1.0]], &one_version(2), 2, &[0.0, 10.0, 20.0], &[user]);
        let mut out = Vec::new();
        assert_eq!(compute(&input, &mut out), Ok((1, 3)));
        assert_eq!(out, vec![98.0, 40.0, 100.0]);
    }

    #[test]
    fn rejects_bad_input_without_panicking() {
        let good = msg(&[[1.0, 0.0, 1.0]], &one_version(1), 1, &[0.0, 1.0],
                       &[([0.0, -1.0], vec![], vec![[0.5, 0.0, 0.0, 1.0, 1.0]])]);
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
        let last = good.len() - 5;
        // Sub index out of range, version out of range, NaN, a pin that is not a whole percent.
        for (offset, v, err) in [(2, 7.0, E_INDEX), (1, 3.0, E_INDEX), (4, f64::NAN, E_VALUE), (2, -3.0, E_INDEX)] {
            let mut b = good.clone();
            b[last + offset] = v;
            assert_eq!(compute(&b, &mut out), Err(err), "offset {offset} value {v}");
        }
        let mut bad_pin = good.clone();
        bad_pin[last + 2] = -2.0;
        bad_pin[last + 3] = 100.5;
        assert_eq!(compute(&bad_pin, &mut out), Err(E_VALUE));
        // Negative/huge counts, a fractional count.
        for (slot, v) in [(5, 1001.0), (5, -1.0), (6, 1e18), (3, 0.5), (4, 1e9)] {
            let mut b = good.clone();
            b[slot] = v;
            assert!(compute(&b, &mut out).is_err(), "slot {slot}");
        }
        let unsorted = msg(&[[1.0, 0.0, 1.0]], &one_version(1), 1, &[1.0, 0.0], &[]);
        assert_eq!(compute(&unsorted, &mut out), Err(E_ORDER));
        let backwards = msg(&[[1.0, 0.0, 1.0]], &one_version(1), 1, &[0.0, 1.0],
                            &[([0.0, -1.0], vec![], vec![[0.5, 0.0, 0.0, 1.0, 1.0], [0.4, 0.0, 0.0, 1.0, 1.0]])]);
        assert_eq!(compute(&backwards, &mut out), Err(E_ORDER));
    }
}
