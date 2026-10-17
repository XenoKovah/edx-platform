"""
Tests for the calculations behind the "Progress Over Time" tab, in lms/djangoapps/instructor/progress_timeline.py.
"""
import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import ddt

from lms.djangoapps.instructor.progress_timeline import (
    BadRequest,
    CourseGradeModel,
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
    The replayed grade is the one on the Progress page.
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

    def test_rounding_matches_the_progress_page(self):
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


class RankAndShapeTest(unittest.TestCase):
    """
    Learners are sent with their scores at the start of the period plus the changes in it, best first.
    """
    def setUp(self):
        super().setUp()
        grader = [{'type': 'Homework', 'min_count': 2, 'drop_count': 0, 'weight': 1.0}]
        subsections = [{'format': 'Homework', 'graded': True}, {'format': 'Homework', 'graded': True}]
        blocks = [
            {'sub': 0, 'weight': None, 'max': 1, 'graded': True},
            {'sub': 1, 'weight': None, 'max': 1, 'graded': True},
        ]
        self.model = CourseGradeModel(grader, subsections, blocks)
        self.window = (1000.0, 2000.0, [1000.0, 1500.0, 2000.0], 500)
        self.rows = {
            1: [(0, 1, 1, 900.0), (1, 1, 1, 1600.0)],   # 50% -> 100%
            2: [(0, 1, 1, 1200.0)],                     # 0% -> 50%, longer ago
            3: [(0, 1, 1, 100.0)],                      # 50% all along
        }
        self.last_active = {1: 1600.0, 2: 1200.0, 3: 100.0}
        self.usernames = {1: 'ann', 2: 'bob', 3: 'cy'}

    def shape(self, **params):
        return rank_and_shape(
            self.model, self.rows, self.usernames, self.window, parse_params(params), self.last_active
        )

    def test_payload(self):
        payload = self.shape()
        assert [(u['username'], u['pct_start'], u['pct_end']) for u in payload['users']] == [
            ('ann', 50, 100), ('bob', 0, 50), ('cy', 50, 50)
        ]
        ann = payload['users'][0]
        assert ann['base'] == [[0, 1.0]]
        assert ann['events'] == [[600.0, 1, 1.0]]
        assert payload['window']['boundaries'] == [0.0, 500.0, 1000.0]
        assert payload['model'] == {'types': [[2, 0, 1.0]], 'subs': [[0, 1.0], [0, 1.0]]}
        assert payload['changed_in_window'] == 2

    def test_most_recently_active_first(self):
        assert [u['username'] for u in self.shape(rank='recent')['users']] == ['ann', 'bob', 'cy']
        self.last_active[2] = 1900.0
        assert [u['username'] for u in self.shape(rank='recent')['users']] == ['bob', 'ann', 'cy']

    def test_learners_without_change_can_be_left_out(self):
        assert [u['username'] for u in self.shape(changed='1')['users']] == ['ann', 'bob']

    def test_limit(self):
        assert [u['username'] for u in self.shape(limit='1')['users']] == ['ann']

    def test_a_score_at_the_first_point_is_part_of_the_start(self):
        self.rows[2] = [(0, 1, 1, 1000.0)]
        bob = [u for u in self.shape()['users'] if u['username'] == 'bob'][0]
        assert (bob['pct_start'], bob['events']) == (50, [])
