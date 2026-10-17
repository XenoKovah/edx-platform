"""
The calculations behind the "Progress Over Time" tab of the instructor dashboard: each learner's course grade (the
percent on their Progress page) at every point of a chosen period.

Grades are replayed from the learners' per-block scores, the way that edx-platform calculates them:
  scores.weighted_score -> subsection graded_total -> compute_percent (4 decimal places)
  -> AssignmentFormatGrader (min_count padding, dropping the lowest drop_count)
  -> WeightedSubsectionsGrader -> CourseGrade._compute_percent (round_away_from_zero(percent * 100 + 0.05) / 100).
The browser does the same replay at every point of the period (progress_timeline_engine.js, or the WebAssembly module
built from progress_timeline_engine/); this module works out the grades at the start and at the end of the period,
which rank the learners.

Every value that comes from the browser goes through parse_params() and is either mapped onto a whitelist or parsed
into a number or a date and range-checked.  Nothing from the request is put into SQL or HTML.

There are no Django imports here, so that the tests can exercise all of it directly.
"""
import datetime as dt
import math
import re
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
    The graded structure of a course: what each scored block is worth, and how subsections make up the grade.

    grader: list of {type, min_count, drop_count, weight}, from the course's grading policy
    subsections: list of {format, graded, staff_only}
    blocks: list of {sub (index into subsections), weight, max, graded, staff_only}
    """

    def __init__(self, grader, subsections, blocks):
        self.grader = [dict(g) for g in grader]
        type_index = {g['type']: i for i, g in enumerate(self.grader)}
        self.block_sub = []
        self.block_counts = []
        self.block_weight = []
        sub_possible = [0.0] * len(subsections)
        for block in blocks:
            counts = (
                bool(block['graded']) and not block.get('staff_only') and
                not subsections[block['sub']].get('staff_only')
            )
            weight = block['weight']
            raw_max = block['max'] or 0
            self.block_sub.append(block['sub'])
            self.block_counts.append(counts)
            self.block_weight.append(weight)
            if counts:
                sub_possible[block['sub']] += float(raw_max) if (weight is None or raw_max == 0) else float(weight)
        # A subsection is on the grade sheet if it is graded, its format is one of the grader's assignment types, and
        # it is worth something (course_grade.graded_subsections_by_format).
        self.sub_possible = sub_possible
        self.sub_type = []
        for i, subsection in enumerate(subsections):
            graded = subsection['graded'] and not subsection.get('staff_only')
            assignment_type = type_index.get(subsection['format']) if graded else None
            if assignment_type is not None and sub_possible[i] <= 0:
                assignment_type = None
            self.sub_type.append(assignment_type)
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

    def percent(self, sub_earned):
        """
        The course grade (0 to 1, rounded like the Progress page) for the points earned in each subsection.
        """
        total = 0.0
        for assignment_type, grader in enumerate(self.grader):
            percents = []
            for i in self.type_subs.get(assignment_type, []):
                possible = self.sub_possible[i]
                percents.append(_round4(sub_earned[i] / possible) if possible > 0 else 0.0)
            count = max(int(grader['min_count']), len(percents))
            percents += [0.0] * (count - len(percents))
            drop = int(grader['drop_count'])
            if drop > 0:
                percents = sorted(percents, reverse=True)[:-drop] if drop < len(percents) else []
                count -= drop
            type_percent = (sum(percents) / count) if count > 0 else 0.0
            total += type_percent * grader['weight']
        return round_away_from_zero(total * 100 + 0.05) / 100


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


def rank_and_shape(model, rows_by_user, usernames, window, opts, last_active):
    """
    Build the response: the period, the grading model, and the chosen learners with their scores.

    rows_by_user: {user_id: [(block_index, raw_earned, raw_possible, modified), ...]}, every scored row of the
                  candidate learners from before the end of the period
    last_active:  {user_id: the latest modified time of their scored rows before the end of the period}

    For each learner, the response has the points earned in each graded subsection at the first point of the period
    ('base') and every later change ('events', seconds after the first point).  The learners are ranked, filtered and
    cut to opts['limit'] here, using their grades at the first and the last point.
    """
    _, end, bounds, step = window
    graded_subs = [i for i, t in enumerate(model.sub_type) if t is not None]
    sub_pos = {s: j for j, s in enumerate(graded_subs)}

    shaped = []
    for user_id, rows in rows_by_user.items():
        base = [0.0] * len(model.sub_type)
        events = []
        for block_index, earned, possible, modified in rows:
            if not model.block_counts[block_index]:
                continue
            j = sub_pos.get(model.block_sub[block_index])
            if j is None:
                continue
            points = model.weighted_earned(block_index, earned, possible)
            if modified <= bounds[0]:
                base[model.block_sub[block_index]] += points
            elif points:
                events.append((modified, j, points))
        events.sort()
        final = list(base)
        for _, j, points in events:
            final[graded_subs[j]] += points
        shaped.append({
            'id': user_id,
            'username': usernames.get(user_id, ''),
            'last_active': last_active.get(user_id),
            'pct_start': round(model.percent(base) * 100),
            'pct_end': round(model.percent(final) * 100),
            'base': [[sub_pos[i], round(base[i], 6)] for i in graded_subs if base[i]],
            'events': [[round(t - bounds[0], 3), j, round(points, 6)] for t, j, points in events],
        })
    changed_count = sum(1 for u in shaped if u['pct_end'] != u['pct_start'])
    if opts['changed_only']:
        # "No change" means the same grade at the first and the last point, even if it moved in between.
        shaped = [u for u in shaped if u['pct_end'] != u['pct_start']]
    if opts['rank'] == 'gain':
        shaped.sort(key=lambda u: (-(u['pct_end'] - u['pct_start']), -(u['last_active'] or 0), u['username']))
    else:
        shaped.sort(key=lambda u: (-(u['last_active'] or 0), u['username']))
    return {
        'window': {
            'start': bounds[0], 'end': end, 'step': step, 'tz': opts['tz'],
            'boundaries': [round(b - bounds[0], 3) for b in bounds],
        },
        'model': {
            'types': [[int(g['min_count']), int(g['drop_count']), float(g['weight'])] for g in model.grader],
            'subs': [[model.sub_type[i], model.sub_possible[i]] for i in graded_subs],
        },
        'users': shaped[:opts['limit']],
        'changed_in_window': changed_count,
    }
