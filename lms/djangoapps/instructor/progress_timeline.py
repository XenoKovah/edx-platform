"""
The calculations behind the "Progress Over Time" tab of the instructor dashboard: each learner's course grade, as
edx-platform recorded it (the grade in the grade report, which certificates go by), at every point of a chosen period.

Grades are replayed from the learners' per-block scores, the way that edx-platform calculates them:
  scores.weighted_score -> subsection graded_total -> compute_percent (4 decimal places)
  -> AssignmentFormatGrader (min_count padding, dropping the lowest drop_count)
  -> WeightedSubsectionsGrader -> CourseGrade._compute_percent (round_away_from_zero(percent * 100 + 0.05) / 100).

Courses change while learners take them, and edx-platform does not regrade anyone when they do: a score change
recalculates that one subsection against the course as published at that moment and stores it, and the course grade is
then worked out from the stored subsection grades (subsections without one are calculated afresh) over the subsections
the course has at that moment.  GradeHistory replays exactly that over the course's published versions, so a learner
who finished the course before units were added stays at the grade they were given, as in the grade report.  (Their
Progress page, which recalculates the course grade from the current subsections, can show less.)  The stored subsection
grades are laid over the replay at the times they were stored, which also brings in what a replay of scores can't see:
whole-course regrades (edx-platform regrades everyone when the grading policy changes), grade overrides, and scores from
before the course's history begins.

The server sends each learner's recorded subsection grades at the start of the period and every later change, each
change tagged with the version of the course it was recorded against.  The browser turns those into a course grade at
every point of the period (the WebAssembly module built from progress_timeline_engine/); this module works out the
grades at the start and at the end, which rank the learners.

Every value that comes from the browser goes through parse_params() and is either mapped onto a whitelist or parsed
into a number or a date and range-checked.  Nothing from the request is put into SQL or HTML.

There are no Django imports here, so that the tests can exercise all of it directly.
"""
import datetime as dt
import math
import re
from bisect import bisect_right
from collections import defaultdict
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

MAX_LEARNERS = 1000
DEFAULT_LEARNERS = 20
MAX_POINTS = 1000
TARGET_POINTS = 30

PERIODS = ('day', 'week', 'month', 'custom')
RANKS = ('gain', 'recent')
HOUR = 3600
DAY = 86400
# Interval name -> seconds; 'auto' picks one from AUTO_LADDER.
STEPS = {'1h': HOUR, '6h': 6 * HOUR, '1d': DAY, '7d': 7 * DAY}
AUTO_LADDER = (HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 28 * DAY)
# Period -> (length, interval): the last day hourly, the last week in 6-hour steps, the last 30 days at midnights.
PERIOD_DEFAULTS = {'day': (DAY, HOUR), 'week': (7 * DAY, 6 * HOUR), 'month': (30 * DAY, DAY)}

_INT_RE = re.compile(r'^[0-9]{1,7}$')
_DATE_RE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
_TZ_RE = re.compile(r'^[A-Za-z0-9_+\-/]{1,64}$')


class BadRequest(ValueError):
    """
    The request can't be answered; the message says why, and is shown to the instructor.
    """


def _round4(x):
    """
    numpy.around(x, 4), as scores.compute_percent uses it: multiply, round half to even, divide.
    """
    return round(x * 10000) / 10000


def round_away_from_zero(x):
    """
    openedx.core.lib.grade_utils.round_away_from_zero, to a whole number.
    """
    return float(math.floor(x + 0.5)) if x >= 0 else float(math.ceil(x - 0.5))


class CourseGradeModel:
    """
    The graded structure of one version of a course: what each scored block is worth, and how subsections make up the
    grade.

    grader: list of {type, min_count, drop_count, weight}, from the course's grading policy
    subsections: list of {format, graded, staff_only, and optionally key}
    blocks: list of {sub (index into subsections), weight, max, graded, staff_only, and optionally key}
    """

    def __init__(self, grader, subsections, blocks):
        self.grader = [dict(g) for g in grader]
        type_index = {g['type']: i for i, g in enumerate(self.grader)}
        self.block_sub = []
        self.block_counts = []
        self.block_weight = []
        self.block_key = []
        self.block_index = {}
        self.sub_blocks = [[] for _ in subsections]
        sub_possible = [0.0] * len(subsections)
        for b, block in enumerate(blocks):
            counts = (
                bool(block['graded']) and not block.get('staff_only') and
                not subsections[block['sub']].get('staff_only')
            )
            weight = block['weight']
            raw_max = block['max'] or 0
            self.block_sub.append(block['sub'])
            self.block_counts.append(counts)
            self.block_weight.append(weight)
            self.block_key.append(block.get('key'))
            if block.get('key') is not None:
                self.block_index[block['key']] = b
            if counts:
                sub_possible[block['sub']] += float(raw_max) if (weight is None or raw_max == 0) else float(weight)
                self.sub_blocks[block['sub']].append(b)
        self.sub_possible = sub_possible
        self.sub_keys = [subsection.get('key') for subsection in subsections]
        # A subsection is on the grade sheet if it is graded and its format is one of the grader's assignment types
        # (sheet_type), and its grade is out of more than nothing (course_grade.graded_subsections_by_format).
        self.sheet_type = []
        self.sub_type = []
        for i, subsection in enumerate(subsections):
            graded = subsection['graded'] and not subsection.get('staff_only')
            assignment_type = type_index.get(subsection['format']) if graded else None
            self.sheet_type.append(assignment_type)
            self.sub_type.append(assignment_type if sub_possible[i] > 0 else None)
        self.type_subs = defaultdict(list)
        for i, assignment_type in enumerate(self.sub_type):
            if assignment_type is not None:
                self.type_subs[assignment_type].append(i)

    def weighted_earned(self, block_index, raw_earned, raw_possible):
        """
        What a raw score on the block is worth towards its subsection (scores.weighted_score).
        """
        weight = self.block_weight[block_index]
        if weight is None or not raw_possible:
            return float(raw_earned)
        return float(raw_earned) * weight / raw_possible

    def sub_earned(self, sub, scores):
        """
        The points earned in a subsection, from {block key: (raw earned, raw possible)}.
        """
        total = 0.0
        for b in self.sub_blocks[sub]:
            score = scores.get(self.block_key[b])
            if score is not None:
                total += self.weighted_earned(b, score[0], score[1])
        return total

    def percent(self, sub_earned):
        """
        The course grade (0 to 1, rounded like edx-platform rounds it) for the points earned in each subsection.
        """
        return grader_percent(self.grader, [
            [_round4(sub_earned[i] / self.sub_possible[i]) for i in self.type_subs.get(t, [])]
            for t in range(len(self.grader))
        ])


def grader_percent(grader, percents_by_type):
    """
    The course grade (0 to 1, rounded like CourseGrade._compute_percent) from each assignment type's subsection
    percents: min_count padding with zeros, the lowest drop_count left out, then the types' weights.
    """
    total = 0.0
    for assignment_type, rule in enumerate(grader):
        percents = list(percents_by_type[assignment_type])
        count = max(int(rule['min_count']), len(percents))
        percents += [0.0] * (count - len(percents))
        drop = int(rule['drop_count'])
        if drop > 0:
            percents = sorted(percents, reverse=True)[:-drop] if drop < len(percents) else []
            count -= drop
        type_percent = (sum(percents) / count) if count > 0 else 0.0
        total += type_percent * rule['weight']
    return round_away_from_zero(total * 100 + 0.05) / 100


# The sheet index of an event that holds the course grade at a stored value (see GradeHistory.replay).
PIN = -2


class GradeHistory:
    """
    A course's graded structure over time, and the grades edx-platform recorded from it.

    snapshots: [(published at, CourseGradeModel)], oldest first, with keys on the subsections and blocks.  The first
    one also stands for any time before it.

    Every subsection that is on the grade sheet of some version gets one number (its "sheet index"), so that a grade
    recorded against one version can be carried into the next, as edx-platform carries stored subsection grades.
    """

    def __init__(self, grader, snapshots):
        self.grader = [dict(g) for g in grader]
        self.starts = [start for start, _ in snapshots]
        self.models = [model for _, model in snapshots]
        self.sheet_index = {}
        self.entries = []      # per version: [(sheet index, assignment type, possible)]
        self.sheet_of = []     # per version: {subsection position: sheet index}
        self.block_keys = set()
        for v, model in enumerate(self.models):
            entries, sheet_of = [], {}
            for i, assignment_type in enumerate(model.sheet_type):
                if assignment_type is None:
                    continue
                key = model.sub_keys[i] if model.sub_keys[i] is not None else (v, i)
                u = self.sheet_index.setdefault(key, len(self.sheet_index))
                entries.append((u, assignment_type, model.sub_possible[i]))
                sheet_of[i] = u
            self.entries.append(entries)
            self.sheet_of.append(sheet_of)
            self.block_keys.update(model.block_index)
        # A course is created empty and filled in afterwards; nobody can have a score before then, but scores from
        # before the history begins (a course that was re-created) are best placed in the first version with something
        # graded.
        self.first_graded = next((v for v, entries in enumerate(self.entries) if entries), 0)

    @property
    def n_sheet(self):
        return len(self.sheet_index)

    def version_at(self, ts):
        """
        The version of the course that was published at the time.
        """
        return max(bisect_right(self.starts, ts) - 1, self.first_graded)

    def percent(self, version, records):
        """
        The course grade (0 to 1) worked out against the version, from {sheet index: (earned, possible)} recorded
        subsection grades; a subsection without one has nothing earned in it, out of its possible in that version.
        """
        by_type = [[] for _ in self.grader]
        for u, assignment_type, possible in self.entries[version]:
            record = records.get(u)
            if record is not None:
                if record[1] > 0:
                    by_type[assignment_type].append(_round4(record[0] / record[1]))
            elif possible > 0:
                by_type[assignment_type].append(0.0)
        return grader_percent(self.grader, by_type)

    def replay(self, rows, stored_subs=(), stored_at=None, stored_percent=None):
        """
        The changes to a learner's recorded grades.

        rows:        their scored rows [(block key, raw earned, raw possible, time)]
        stored_subs: their stored subsection grades [(time stored, subsection key, earned, possible)]
        stored_at:   when their course grade was last stored, or None
        stored_percent: that stored course grade (0 to 1), or None

        Returns [(time, version, sheet index or PIN or -1, earned, possible)] in time order.  Each score recalculates
        its subsection against the version published at that time, and the course grade against the same version; a
        stored subsection grade replaces the recalculated one from the time it was stored; -1 is a recalculation that
        recorded nothing new but moved the course grade onto another version.  The last course grade stored is the last
        word: at the end, the stored subsection grades and the version of that time stand, and if they still don't come
        to the stored course grade (the grading policy changed since, say), a PIN event (earned = the stored grade)
        holds the course grade at that, as the grade report shows it.
        """
        items = [(ts, 0, key, earned, possible) for key, earned, possible, ts in rows]
        stored = {}
        for ts, key, earned, possible in stored_subs:
            if key in self.sheet_index:
                items.append((ts, 1, key, earned, possible))
                stored[self.sheet_index[key]] = (float(earned), float(possible))
        if stored_at is not None:
            items.append((stored_at, 2, None, 0.0, 0.0))
        items.sort(key=lambda item: (item[0], item[1]))

        scores, records, events = {}, {}, []
        version = None
        for ts, kind, key, earned, possible in items:
            v = self.version_at(ts)
            model = self.models[v]
            changes = []
            if kind == 0:
                scores[key] = (earned, possible)
                b = model.block_index.get(key)
                u = self.sheet_of[v].get(model.block_sub[b]) if b is not None else None
                if u is not None:
                    record = (model.sub_earned(model.block_sub[b], scores), model.sub_possible[model.block_sub[b]])
                    if records.get(u) != record:
                        records[u] = record
                        changes.append(u)
            elif kind == 1:
                u = self.sheet_index[key]
                record = (float(earned), float(possible))
                if records.get(u) != record:
                    records[u] = record
                    changes.append(u)
            if v != version:
                # Subsections with no stored grade are calculated afresh; a block moved into one can make it count.
                for sub, u in self.sheet_of[v].items():
                    if u not in records:
                        earned_here = model.sub_earned(sub, scores)
                        if earned_here:
                            records[u] = (earned_here, model.sub_possible[sub])
                            changes.append(u)
            events.extend((ts, v, u, records[u][0], records[u][1]) for u in changes)
            if not changes and v != version:
                events.append((ts, v, -1, 0.0, 0.0))
            version = v
        if stored_at is not None and items:
            last = max(items[-1][0], stored_at)
            v = self.version_at(stored_at)
            changes = [u for u, record in stored.items() if records.get(u) != record]
            for u in changes:
                records[u] = stored[u]
            events.extend((last, v, u, records[u][0], records[u][1]) for u in changes)
            if not changes and v != version:
                events.append((last, v, -1, 0.0, 0.0))
            if stored_percent is not None and round(stored_percent * 100) != round(self.percent(v, records) * 100):
                events.append((last, v, PIN, float(stored_percent), 0.0))
        return events


@lru_cache(maxsize=1)
def _timezones():
    return frozenset(available_timezones())


def _one(params, name, default=None):
    """
    The single value of a query parameter (the last one, if it was repeated), or the default.
    """
    value = params.get(name, default)
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else default
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > 64:
        raise BadRequest(f'invalid {name}')
    return value.strip()


def parse_params(params):
    """
    Validate the query string (a QueryDict, or a dict of strings or lists of strings).

    Returns a dict of safe, typed values.  Raises BadRequest for anything outside the allowed values.
    """
    period = _one(params, 'period', 'day')
    if period not in PERIODS:
        raise BadRequest('period must be one of ' + ', '.join(PERIODS))
    rank = _one(params, 'rank', 'gain')
    if rank not in RANKS:
        raise BadRequest('rank must be one of ' + ', '.join(RANKS))
    step = _one(params, 'step', 'auto')
    if step != 'auto' and step not in STEPS:
        raise BadRequest('step must be auto or one of ' + ', '.join(STEPS))
    limit_text = _one(params, 'limit', str(DEFAULT_LEARNERS))
    if not _INT_RE.match(limit_text) or not 1 <= int(limit_text) <= MAX_LEARNERS:
        raise BadRequest(f'limit must be a whole number from 1 to {MAX_LEARNERS}')
    changed = _one(params, 'changed', '0')
    if changed not in ('0', '1'):
        raise BadRequest('changed must be 0 or 1')
    tz_name = _one(params, 'tz', 'UTC')
    if not _TZ_RE.match(tz_name) or tz_name not in _timezones():
        tz_name = 'UTC'
    start_date = end_date = None
    if period == 'custom':
        start_text, end_text = _one(params, 'start', ''), _one(params, 'end', '')
        if not (_DATE_RE.match(start_text) and _DATE_RE.match(end_text)):
            raise BadRequest('custom range needs start and end dates (YYYY-MM-DD)')
        try:
            start_date = dt.date.fromisoformat(start_text)
            end_date = dt.date.fromisoformat(end_text)
        except ValueError as exc:
            raise BadRequest('invalid date') from exc
        if end_date < start_date:
            raise BadRequest('end date is before start date')
    return {
        'period': period, 'rank': rank, 'step': step, 'limit': int(limit_text), 'tz': tz_name,
        'changed_only': changed == '1', 'start_date': start_date, 'end_date': end_date,
    }


def _local_floor(ts, step, tz):
    """
    The latest boundary at or before ts on the local clock, for a step that divides a day (or is whole days).
    """
    local = dt.datetime.fromtimestamp(ts, tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if step >= DAY:
        return midnight
    since = (local - midnight).total_seconds()
    return midnight + dt.timedelta(seconds=(since // step) * step)


def _boundaries(start_ts, end_ts, step, tz):
    """
    The points of the chart: whole hours, 6-hour marks or midnights on the local clock, from the last one at or before
    the start, then the end itself ("now") as the last point.  Stepping the wall clock keeps midnights at midnight
    across daylight-saving changes.
    """
    out = []
    wall = _local_floor(start_ts, step, tz).replace(tzinfo=None)
    delta = dt.timedelta(seconds=step)
    while True:
        ts = wall.replace(tzinfo=tz).timestamp()
        if ts >= end_ts:
            break
        if not out or ts > out[-1]:
            out.append(ts)
        if len(out) > MAX_POINTS:
            raise BadRequest(f'that range would need more than {MAX_POINTS} points; choose a longer interval')
        wall += delta
    if not out or end_ts > out[-1]:
        out.append(end_ts)
    return out


def build_window(opts, now_ts, data_start_ts):
    """
    Return (start, end, boundaries, step) of the period, as Unix times and seconds, for validated options.

    data_start_ts is when the course's data begins (or None); custom ranges are clipped to it and to now.
    """
    tz = ZoneInfo(opts['tz'])
    if opts['period'] == 'custom':
        start = dt.datetime.combine(opts['start_date'], dt.time(), tz).timestamp()
        end = dt.datetime.combine(opts['end_date'] + dt.timedelta(days=1), dt.time(), tz).timestamp()
        if data_start_ts:
            start = max(start, _local_floor(data_start_ts, DAY, tz).timestamp())
        end = min(end, now_ts)
        if end <= start:
            raise BadRequest('there is no learner data in that range')
        span = end - start
        default_step = None
    else:
        span, default_step = PERIOD_DEFAULTS[opts['period']]
        end = now_ts
        start = now_ts - span
    if opts['step'] != 'auto':
        step = STEPS[opts['step']]
    elif default_step:
        step = default_step
    else:
        step = next((s for s in AUTO_LADDER if span / s <= TARGET_POINTS), AUTO_LADDER[-1])
    bounds = _boundaries(start, end, step, tz)
    return bounds[0], end, bounds, step


def rank_and_shape(history, rows_by_user, usernames, window, opts, last_active, stored_by_user=None):
    """
    Build the response: the period, the grading model, and the chosen learners with their recorded grades.

    history:      GradeHistory of the course
    rows_by_user: {user_id: [(block key, raw_earned, raw_possible, modified), ...]}, every scored row of the
                  candidate learners from before the end of the period
    last_active:  {user_id: the latest modified time of their scored rows before the end of the period}
    stored_by_user: {user_id: ([(time, subsection key, earned, possible)], course grade stored at or None, that
                  course grade or None)}, the grades edx-platform stored before the end of the period (see
                  GradeHistory.replay)

    For each learner, the response has their recorded subsection grades at the first point of the period and the
    version of the course their course grade was worked out against ('base'), and every later change ('events',
    seconds after the first point).  The learners are ranked, filtered and cut to opts['limit'] here, using their
    grades at the first and the last point.
    """
    _, end, bounds, step = window
    first = bounds[0]
    shaped = []
    for user_id, rows in rows_by_user.items():
        base, version, later = {}, history.version_at(first), []
        stored_subs, stored_at, stored_percent = (stored_by_user or {}).get(user_id, ((), None, None))
        pin = None
        for event in history.replay(rows, stored_subs, stored_at, stored_percent):
            ts, v, u, earned, possible = event
            if ts <= first:
                version = v
                pin = earned if u == PIN else None
                if u >= 0:
                    base[u] = (earned, possible)
            else:
                later.append(event)
        final, final_version, final_pin = dict(base), version, pin
        for _, v, u, earned, possible in later:
            final_version = v
            final_pin = earned if u == PIN else None
            if u >= 0:
                final[u] = (earned, possible)
        shaped.append({
            'id': user_id,
            'username': usernames.get(user_id, ''),
            'last_active': last_active.get(user_id),
            'pct_start': round((history.percent(version, base) if pin is None else pin) * 100),
            'pct_end': round((history.percent(final_version, final) if final_pin is None else final_pin) * 100),
            '_version': version,
            '_pin': pin,
            '_base': base,
            '_events': later,
        })
    changed_count = sum(1 for u in shaped if u['pct_end'] != u['pct_start'])
    if opts['changed_only']:
        # "No change" means the same grade at the first and the last point, even if it moved in between.
        shaped = [u for u in shaped if u['pct_end'] != u['pct_start']]
    if opts['rank'] == 'gain':
        shaped.sort(key=lambda u: (-(u['pct_end'] - u['pct_start']), -(u['last_active'] or 0), u['username']))
    else:
        shaped.sort(key=lambda u: (-(u['last_active'] or 0), u['username']))
    shaped = shaped[:opts['limit']]

    # Only the versions and subsections these learners' grades use go to the browser, renumbered from 0.
    versions = sorted({u['_version'] for u in shaped} | {e[1] for u in shaped for e in u['_events']})
    version_pos = {v: i for i, v in enumerate(versions)}
    sheet = sorted({entry[0] for v in versions for entry in history.entries[v]})
    sheet_pos = {u: i for i, u in enumerate(sheet)}
    users = []
    for u in shaped:
        users.append({
            'id': u['id'],
            'username': u['username'],
            'last_active': u['last_active'],
            'pct_start': u['pct_start'],
            'pct_end': u['pct_end'],
            'base': {
                'version': version_pos[u['_version']],
                'pin': None if u['_pin'] is None else round(u['_pin'] * 100),
                'subs': [[sheet_pos[s], round(e, 6), round(p, 6)] for s, (e, p) in sorted(u['_base'].items())
                         if s in sheet_pos],
            },
            # A grade for a subsection none of these versions has still moves the learner onto its version.
            'events': [
                [round(ts - first, 3), version_pos[v], s, round(e * 100), 0] if s == PIN else
                [round(ts - first, 3), version_pos[v], sheet_pos.get(s, -1), round(e, 6), round(p, 6)]
                for ts, v, s, e, p in u['_events']
            ],
        })
    return {
        'window': {
            'start': first, 'end': end, 'step': step, 'tz': opts['tz'],
            'boundaries': [round(b - first, 3) for b in bounds],
        },
        'model': {
            'types': [[int(g['min_count']), int(g['drop_count']), float(g['weight'])] for g in history.grader],
            'subs': len(sheet),
            'versions': [
                [[sheet_pos[s], t, round(p, 6)] for s, t, p in history.entries[v]] for v in versions
            ],
        },
        'users': users,
        'changed_in_window': changed_count,
    }
