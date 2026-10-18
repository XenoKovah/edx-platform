"""
Tests for the calculations behind the "Progress Over Time" tab, in lms/djangoapps/instructor/progress_timeline.py.
"""
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import ddt

from lms.djangoapps.instructor.progress_timeline import (
    PIN,
    BadRequest,
    CourseGradeModel,
    GradeHistory,
    build_window,
    parse_params,
    rank_and_shape
)

UTC = dt.timezone.utc


@ddt.ddt
class ParseParamsTest(unittest.TestCase):
    """
    Everything from the browser is checked before it is used.
    """
    @ddt.data('0', '1001', '-1', '1e3', '20.5', ' ', '9' * 8, '١٢', '0x10', '', 'x' * 100)
    def test_bad_learner_counts_are_refused(self, limit):
        with self.assertRaises(BadRequest):
            parse_params({'limit': limit})

    def test_learner_count(self):
        assert parse_params({})['limit'] == 20
        assert parse_params({'limit': '1'})['limit'] == 1
        assert parse_params({'limit': '1000'})['limit'] == 1000
        assert parse_params({'limit': ' 35 '})['limit'] == 35

    @ddt.data(('period', 'year'), ('rank', 'name'), ('step', '5m'), ('changed', 'yes'))
    @ddt.unpack
    def test_values_outside_the_lists_are_refused(self, name, value):
        with self.assertRaises(BadRequest):
            parse_params({name: value})

    def test_time_zone(self):
        assert parse_params({})['tz'] == 'UTC'
        assert parse_params({'tz': 'America/New_York'})['tz'] == 'America/New_York'
        assert parse_params({'tz': '../../etc/passwd'})['tz'] == 'UTC'
        assert parse_params({'tz': 'Not/A_Zone'})['tz'] == 'UTC'

    def test_the_last_of_repeated_values_counts(self):
        assert parse_params({'limit': ['5', '7']})['limit'] == 7

    def test_changed_only(self):
        assert parse_params({})['changed_only'] is False
        assert parse_params({'changed': '1'})['changed_only'] is True

    @ddt.data(
        {'start': '2026-02-30', 'end': '2026-03-01'},
        {'start': '2026-03-02', 'end': '2026-03-01'},
        {'start': '2026-03-02'},
        {'start': '02/03/2026', 'end': '2026-03-01'},
    )
    def test_bad_custom_ranges_are_refused(self, dates):
        with self.assertRaises(BadRequest):
            parse_params(dict(dates, period='custom'))

    def test_custom_range(self):
        opts = parse_params({'period': 'custom', 'start': '2026-03-01', 'end': '2026-03-02'})
        assert (opts['start_date'], opts['end_date']) == (dt.date(2026, 3, 1), dt.date(2026, 3, 2))


class BuildWindowTest(unittest.TestCase):
    """
    The points of the chart: hours, 6-hour marks or midnights on the viewer's clock, then "now".
    """
    tz = 'America/New_York'
    data_start = dt.datetime(2021, 1, 1, tzinfo=UTC).timestamp()

    def window(self, now, **params):
        return build_window(parse_params(dict(params, tz=self.tz)), now.timestamp(), self.data_start)

    def test_last_day_is_hourly_then_now(self):
        now = dt.datetime(2026, 9, 30, 11, 54, tzinfo=UTC)
        _, end, bounds, step = self.window(now, period='day')
        assert step == 3600
        assert len(bounds) == 26
        assert bounds[-1] == end == now.timestamp()
        assert all(dt.datetime.fromtimestamp(b, ZoneInfo(self.tz)).minute == 0 for b in bounds[:-1])

    def test_last_week_is_six_hourly(self):
        now = dt.datetime(2026, 9, 30, 11, 54, tzinfo=UTC)
        _, _, bounds, step = self.window(now, period='week')
        assert step == 6 * 3600
        assert 28 <= len(bounds) <= 30

    def test_last_month_is_midnights_even_across_a_clock_change(self):
        now = dt.datetime(2026, 11, 20, 15, 30, tzinfo=ZoneInfo(self.tz))  # the clocks went back on 1 November
        _, end, bounds, _ = self.window(now, period='month')
        midnights = [dt.datetime.fromtimestamp(b, ZoneInfo(self.tz)) for b in bounds[:-1]]
        assert all((t.hour, t.minute) == (0, 0) for t in midnights)
        assert len(bounds) == 32
        assert bounds[-1] == end

    def test_custom_range_is_clipped_to_the_data_and_to_now(self):
        now = dt.datetime(2026, 9, 30, 11, 54, tzinfo=UTC)
        start, end, _, _ = self.window(now, period='custom', start='2019-01-01', end='2027-01-01', step='7d')
        assert start == dt.datetime(2020, 12, 31, tzinfo=ZoneInfo(self.tz)).timestamp()
        assert end == now.timestamp()

    def test_custom_range_picks_an_interval_for_about_30_points(self):
        now = dt.datetime(2026, 9, 30, 11, 54, tzinfo=UTC)
        _, _, bounds, step = self.window(now, period='custom', start='2026-01-01', end='2026-06-30')
        assert step == 7 * 86400
        assert len(bounds) <= 31

    def test_too_many_points_are_refused(self):
        now = dt.datetime(2026, 9, 30, tzinfo=UTC)
        with self.assertRaises(BadRequest):
            self.window(now, period='custom', start='2021-01-01', end='2026-09-01', step='1h')

    def test_a_range_without_data_is_refused(self):
        now = dt.datetime(2026, 9, 30, tzinfo=UTC)
        with self.assertRaises(BadRequest):
            self.window(now, period='custom', start='2027-01-01', end='2027-02-01')


class CourseGradeModelTest(unittest.TestCase):
    """
    Grades are worked out the way edx-platform works them out.
    """
    def model(self, grader, n_subsections=4, **block):
        subsections = [{'format': 'Homework', 'graded': True} for _ in range(n_subsections)]
        blocks = [dict({'sub': i, 'weight': None, 'max': 1, 'graded': True}, **block) for i in range(n_subsections)]
        return CourseGradeModel(grader, subsections, blocks)

    def test_min_count_pads_with_zeros(self):
        model = self.model([{'type': 'Homework', 'min_count': 8, 'drop_count': 0, 'weight': 1.0}])
        assert model.percent([1, 1, 1, 1]) == 0.5

    def test_the_lowest_are_dropped(self):
        model = self.model([{'type': 'Homework', 'min_count': 0, 'drop_count': 1, 'weight': 1.0}])
        assert model.percent([1, 1, 1, 0]) == 1.0

    def test_weights_of_assignment_types(self):
        grader = [
            {'type': 'Homework', 'min_count': 0, 'drop_count': 0, 'weight': 0.99},
            {'type': 'Feedback', 'min_count': 1, 'drop_count': 0, 'weight': 0.01},
        ]
        model = self.model(grader)
        assert model.percent([1, 1, 1, 1]) == 0.99

    def test_rounding_matches_edx_platform(self):
        model = self.model([{'type': 'Homework', 'min_count': 0, 'drop_count': 0, 'weight': 1.0}], n_subsections=3)
        # 2/3 = 66.67% -> round_away_from_zero(66.67 + 0.05) = 67
        assert model.percent([1, 1, 0]) == 0.67

    def test_block_weights(self):
        model = self.model([{'type': 'Homework', 'min_count': 0, 'drop_count': 0, 'weight': 1.0}], weight=10, max=4)
        assert model.weighted_earned(0, 2, 4) == 5.0
        assert model.sub_possible == [10.0] * 4

    def test_ungraded_and_staff_only_blocks_do_not_count(self):
        grader = [{'type': 'Homework', 'min_count': 0, 'drop_count': 0, 'weight': 1.0}]
        subsections = [{'format': 'Homework', 'graded': True}, {'format': 'Homework', 'graded': True}]
        blocks = [
            {'sub': 0, 'weight': None, 'max': 1, 'graded': True},
            {'sub': 1, 'weight': None, 'max': 1, 'graded': False},
            {'sub': 1, 'weight': None, 'max': 1, 'graded': True, 'staff_only': True},
        ]
        model = CourseGradeModel(grader, subsections, blocks)
        assert model.block_counts == [True, False, False]
        assert model.sub_type == [0, None]  # a subsection worth nothing is not on the grade sheet


HOMEWORK = [{'type': 'Homework', 'min_count': 0, 'drop_count': 0, 'weight': 1.0}]


def homework_model(names, grader=None):
    """
    A version of a course with one graded homework subsection per name, each with one 1-point problem; the
    subsection's key is its name and the problem's key is the name in capitals.
    """
    subsections = [{'key': name, 'format': 'Homework', 'graded': True} for name in names]
    blocks = [
        {'key': name.upper(), 'sub': i, 'weight': None, 'max': 1, 'graded': True} for i, name in enumerate(names)
    ]
    return CourseGradeModel(grader or HOMEWORK, subsections, blocks)


class GradeHistoryTest(unittest.TestCase):
    """
    Grades are recorded against the course as it was at the time, and stay as they were recorded.
    """
    def setUp(self):
        super().setUp()
        # Published with homework a and b at 100; homework c was added at 1000.
        self.history = GradeHistory(HOMEWORK, [(100.0, homework_model('ab')), (1000.0, homework_model('abc'))])

    def grade_at(self, events, ts):
        """
        The course grade (whole percent) after the events up to the time.
        """
        records, version, pin = {}, self.history.version_at(ts), None
        for when, v, u, earned, possible in events:
            if when > ts:
                break
            version, pin = v, (earned if u == PIN else None)
            if u >= 0:
                records[u] = (earned, possible)
        return round((self.history.percent(version, records) if pin is None else pin) * 100)

    def test_a_learner_who_finished_before_the_course_grew_keeps_their_grade(self):
        events = self.history.replay([('A', 1, 1, 200.0), ('B', 1, 1, 300.0)])

        assert [(ts, v, u) for ts, v, u, _, _ in events] == [(200.0, 0, 0), (300.0, 0, 1)]
        assert self.grade_at(events, 300.0) == 100
        assert self.grade_at(events, 5000.0) == 100

    def test_the_next_score_is_worked_out_against_the_course_of_its_time(self):
        events = self.history.replay([('A', 1, 1, 200.0), ('B', 1, 1, 300.0), ('C', 0, 1, 1500.0)])

        assert events[-1] == (1500.0, 1, 2, 0.0, 1.0)
        assert self.grade_at(events, 1500.0) == 67

    def test_a_learner_who_starts_after_the_course_grew(self):
        events = self.history.replay([('A', 1, 1, 1200.0)])

        assert self.grade_at(events, 1200.0) == 33

    def test_scores_from_before_the_history_begins_go_to_the_first_version(self):
        events = self.history.replay([('A', 1, 1, 50.0), ('B', 1, 1, 60.0)])

        assert [v for _, v, _, _, _ in events] == [0, 0]
        assert self.grade_at(events, 60.0) == 100

    def test_stored_grades_replace_the_replayed_ones_from_when_they_were_stored(self):
        # A regrade at 2000 stored homework a as half right, and the course grade against the course of that time.
        stored = [(300.0, 'a', 1.0, 1.0), (300.0, 'b', 1.0, 1.0), (2000.0, 'a', 0.5, 1.0)]
        events = self.history.replay([('A', 1, 1, 200.0), ('B', 1, 1, 300.0)], stored, 2000.0, 0.5)

        assert self.grade_at(events, 1999.0) == 100
        assert events[-1] == (2000.0, 1, 0, 0.5, 1.0)
        assert self.grade_at(events, 2000.0) == 50

    def test_a_stored_course_grade_the_subsections_do_not_come_to_is_kept(self):
        events = self.history.replay([('A', 1, 1, 200.0), ('B', 1, 1, 300.0)], [], 300.0, 0.98)

        assert events[-1] == (300.0, 0, PIN, 0.98, 0.0)
        assert self.grade_at(events, 299.0) == 50
        assert self.grade_at(events, 300.0) == 98

    def test_the_stored_course_grade_is_the_last_word(self):
        # A later save of a problem's state that changed nothing does not undo it.
        events = self.history.replay([('A', 1, 1, 200.0), ('B', 1, 1, 300.0), ('B', 1, 1, 900.0)], [], 300.0, 0.98)

        assert events[-1] == (900.0, 0, PIN, 0.98, 0.0)
        assert self.grade_at(events, 900.0) == 98


class RankAndShapeTest(unittest.TestCase):
    """
    Learners are sent with their recorded grades at the start of the period plus the changes in it, best first.
    """
    def setUp(self):
        super().setUp()
        grader = [{'type': 'Homework', 'min_count': 2, 'drop_count': 0, 'weight': 1.0}]
        self.history = GradeHistory(grader, [(0.0, homework_model('ab', grader))])
        self.window = (1000.0, 2000.0, [1000.0, 1500.0, 2000.0], 500)
        self.rows = {
            1: [('A', 1, 1, 900.0), ('B', 1, 1, 1600.0)],   # 50% -> 100%
            2: [('A', 1, 1, 1200.0)],                       # 0% -> 50%, longer ago
            3: [('A', 1, 1, 100.0)],                        # 50% all along
        }
        self.stored = None
        self.last_active = {1: 1600.0, 2: 1200.0, 3: 100.0}
        self.usernames = {1: 'ann', 2: 'bob', 3: 'cy'}

    def shape(self, **params):
        return rank_and_shape(
            self.history, self.rows, self.usernames, self.window, parse_params(params), self.last_active, self.stored
        )

    def test_payload(self):
        payload = self.shape()
        assert [(u['username'], u['pct_start'], u['pct_end']) for u in payload['users']] == [
            ('ann', 50, 100), ('bob', 0, 50), ('cy', 50, 50)
        ]
        ann = payload['users'][0]
        assert ann['base'] == {'version': 0, 'pin': None, 'subs': [[0, 1.0, 1.0]]}
        assert ann['events'] == [[600.0, 0, 1, 1.0, 1.0]]
        assert payload['window']['boundaries'] == [0.0, 500.0, 1000.0]
        assert payload['model'] == {'types': [[2, 0, 1.0]], 'subs': 2, 'versions': [[[0, 0, 1.0], [1, 0, 1.0]]]}
        assert payload['changed_in_window'] == 2

    def test_versions_and_pins_are_sent(self):
        self.history = GradeHistory(HOMEWORK, [(0.0, homework_model('ab')), (1100.0, homework_model('abc'))])
        self.rows = {1: [('A', 1, 1, 900.0), ('B', 1, 1, 950.0), ('C', 1, 1, 1600.0)]}
        # The grading policy changed after the last score, and edx-platform regraded the course: 98%.
        self.stored = {1: ([], 1700.0, 0.98)}
        self.last_active = {1: 1600.0}

        payload = self.shape()

        ann = payload['users'][0]
        assert (ann['pct_start'], ann['pct_end']) == (100, 98)
        assert ann['base'] == {'version': 0, 'pin': None, 'subs': [[0, 1.0, 1.0], [1, 1.0, 1.0]]}
        assert ann['events'] == [[600.0, 1, 2, 1.0, 1.0], [700.0, 1, PIN, 98, 0]]
        assert payload['model']['versions'] == [[[0, 0, 1.0], [1, 0, 1.0]], [[0, 0, 1.0], [1, 0, 1.0], [2, 0, 1.0]]]

    def test_a_pin_before_the_period_is_part_of_the_start(self):
        self.rows = {1: [('A', 1, 1, 900.0), ('B', 1, 1, 950.0)]}
        self.stored = {1: ([], 950.0, 0.98)}
        self.last_active = {1: 950.0}

        ann = self.shape()['users'][0]

        assert (ann['pct_start'], ann['pct_end'], ann['base']['pin'], ann['events']) == (98, 98, 98, [])

    def test_only_the_versions_the_learners_use_are_sent(self):
        self.history = GradeHistory(HOMEWORK, [(0.0, homework_model('x')), (50.0, homework_model('ab'))])

        payload = self.shape()

        assert payload['model']['subs'] == 2
        assert payload['model']['versions'] == [[[0, 0, 1.0], [1, 0, 1.0]]]
        assert all(u['base']['version'] == 0 for u in payload['users'])

    def test_most_recently_active_first(self):
        assert [u['username'] for u in self.shape(rank='recent')['users']] == ['ann', 'bob', 'cy']
        self.last_active[2] = 1900.0
        assert [u['username'] for u in self.shape(rank='recent')['users']] == ['bob', 'ann', 'cy']

    def test_learners_without_change_can_be_left_out(self):
        assert [u['username'] for u in self.shape(changed='1')['users']] == ['ann', 'bob']

    def test_limit(self):
        assert [u['username'] for u in self.shape(limit='1')['users']] == ['ann']

    def test_a_score_at_the_first_point_is_part_of_the_start(self):
        self.rows[2] = [('A', 1, 1, 1000.0)]
        bob = [u for u in self.shape()['users'] if u['username'] == 'bob'][0]
        assert (bob['pct_start'], bob['events']) == (50, [])
