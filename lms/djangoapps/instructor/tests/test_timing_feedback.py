"""
Tests for the completion-time statistics in lms/djangoapps/instructor/timing_feedback.py.
"""
import itertools
import unittest

import ddt

from lms.djangoapps.instructor import timing_feedback
from lms.djangoapps.instructor.timing_feedback import TimingSection

# The problem that the "timing_feedback" content library holds, with fewer options in its dropdowns.
TIMING_PROBLEM_XML = """<problem>
  <p>What is the total time you spent working on this section (in HH:MM form)? </p>
  <label> Hours: </label>
  <optionresponse>
    <optioninput>
      <option correct="True">00</option>
      <option correct="False">01</option>
      <option correct="False">02</option>
    </optioninput>
  </optionresponse>
  <label> Minutes: </label>
  <optionresponse>
    <optioninput>
      <option correct="True">00</option>
      <option correct="False">01</option>
      <option correct="False">02</option>
    </optioninput>
  </optionresponse>
</problem>"""

MINUTES_FIRST_PROBLEM_XML = TIMING_PROBLEM_XML.replace('Hours:', 'TEMP').replace('Minutes:', 'Hours:').replace(
    'TEMP', 'Minutes:'
)


block_numbers = itertools.count(1)


class FakeBlock:
    """
    The parts of an XBlock that find_timing_sections looks at.
    """
    def __init__(self, category, display_name, children=(), graded_format=None, data=''):
        self.category = category
        self.display_name_with_default = display_name
        self.format = graded_format
        self.data = data
        self.location = f'block-v1:Org+Course+Run+type@{category}+block@{next(block_numbers)}'
        self._children = list(children)

    def get_children(self):
        return self._children


def timing_sequential(name, graded_format='TimingFeedback', problem_xml=TIMING_PROBLEM_XML, problems=1):
    """
    A Timing Feedback subsection, laid out the way that the courses do it: the problem is in a library content block.
    """
    problem_blocks = [FakeBlock('problem', f'{name} problem {number}', data=problem_xml) for number in range(problems)]
    library_content = FakeBlock('library_content', 'timing_feedback', problem_blocks)
    vertical = FakeBlock('vertical', name, [library_content, FakeBlock('discussion', 'discussion')])
    return FakeBlock('sequential', name, [vertical], graded_format=graded_format)


def course_of(*chapters):
    """
    A course made of chapters, each of which is a (name, [sequentials]) pair.
    """
    return FakeBlock('course', 'course', [FakeBlock('chapter', name, sequentials) for name, sequentials in chapters])


@ddt.ddt
class TimingCandidateTest(unittest.TestCase):
    """
    Tests for is_timing_candidate.
    """
    @ddt.data(
        ('Timing Feedback 01: Introduction', None),
        ('\u23f1\ufe0fTiming Feedback 00 - CPU Fundamentals', 'Timing Feedback'),
        ('Timing feedback 01 - Intro & Download & Install', 'Timing Feedback'),
        ('Time Collection - SBO', 'TimingFeedback'),
        ('Time Collection A-C - What, Why, and How', None),
        ('Timing Collection 01 - Download & Install', None),
        ('Logic Analyzer Setup Timing Feedback', ''),
        ('Beta-tester Time Collection', None),
        ('A name that says nothing', 'TimingFeedback'),
        ('A name that says nothing', 'Timing Feedback'),
    )
    @ddt.unpack
    def test_candidates(self, name, graded_format):
        assert timing_feedback.is_timing_candidate(name, graded_format)

    @ddt.data(
        ('Class Introduction', None),
        ('Background: Endianness', 'ProgressMarker'),
        ('Optional Refresher: binary to hex to decimal', ''),
        ('Examples: Choose Your Own Adventure!', 'CVEs'),
        ('', None),
        (None, None),
    )
    @ddt.unpack
    def test_not_candidates(self, name, graded_format):
        assert not timing_feedback.is_timing_candidate(name, graded_format)


class HoursMinutesProblemTest(unittest.TestCase):
    """
    Tests for hours_first_in_problem.
    """
    def test_timing_problem(self):
        assert timing_feedback.hours_first_in_problem(TIMING_PROBLEM_XML) is True

    def test_minutes_first(self):
        assert timing_feedback.hours_first_in_problem(MINUTES_FIRST_PROBLEM_XML) is False

    def test_labels_with_markup_and_entities(self):
        xml = '<problem><label><b>&nbsp;Hours&nbsp;:</b></label><label>Minutes</label></problem>'
        assert timing_feedback.hours_first_in_problem(xml) is True

    def test_problem_that_only_talks_about_hours_and_minutes(self):
        xml = '<problem><p>How many hours and minutes are there in a day?</p><label>Answer</label></problem>'
        assert timing_feedback.hours_first_in_problem(xml) is None

    def test_only_one_of_the_two(self):
        assert timing_feedback.hours_first_in_problem('<problem><label>Hours:</label></problem>') is None

    def test_no_problem(self):
        assert timing_feedback.hours_first_in_problem('') is None
        assert timing_feedback.hours_first_in_problem(None) is None


@ddt.ddt
class SectionLabelTest(unittest.TestCase):
    """
    Tests for section_label, with names from real courses.
    """
    @ddt.data(
        ('\u23f1\ufe0fTiming Feedback 00 - CPU Fundamentals', '00 - CPU Fundamentals'),
        ('Timing Feedback 01: Introduction', '01: Introduction'),
        ('Timing feedback 01 - Intro & Download & Install', '01 - Intro & Download & Install'),
        ('Timing Feedback - Setup Debug Environment', 'Setup Debug Environment'),
        ('Timing Collection 02 - Create Test Executable', '02 - Create Test Executable'),
        ('Time Collection - SBO', 'SBO'),
        ('Time Collection A-C - What, Why, and How', 'A-C - What, Why, and How'),
        (
            'Time Collection B. Setting up a TPM2 development environment',
            'B. Setting up a TPM2 development environment',
        ),
        ('Timing Feedback: Mini-Course Complete!', 'Mini-Course Complete!'),
        ('Logic Analyzer Setup Timing Feedback', 'Logic Analyzer Setup'),
        ('Beta-tester Timing Feedback - Class Introduction', 'Class Introduction'),
        ('A name that says nothing', 'A name that says nothing'),
    )
    @ddt.unpack
    def test_label(self, subsection_name, expected):
        assert timing_feedback.section_label(subsection_name, 'The chapter') == expected

    def test_nothing_but_timing_feedback_is_labeled_with_the_chapter(self):
        assert timing_feedback.section_label('Timing Feedback', 'Download & Install') == 'Download & Install'
        assert timing_feedback.section_label('\u23f1\ufe0f Timing Feedback -', ' Setup ') == 'Setup'

    def test_nothing_at_all(self):
        assert timing_feedback.section_label('Timing Feedback', '') == 'Timing Feedback'


class FindTimingSectionsTest(unittest.TestCase):
    """
    Tests for find_timing_sections.
    """
    def test_finds_the_problems_in_course_order(self):
        first = timing_sequential('Timing Feedback 01: Introduction')
        course = course_of(
            ('Introduction', [FakeBlock('sequential', 'Class Introduction', graded_format='ProgressMarker'), first]),
            ('Registers', [timing_sequential('Timing Feedback 02: Registers', graded_format='Timing Feedback')]),
        )
        sections = timing_feedback.find_timing_sections(course)
        assert [section.label for section in sections] == ['01: Introduction', '02: Registers']
        assert [section.sequential for section in sections] == [
            'Timing Feedback 01: Introduction', 'Timing Feedback 02: Registers'
        ]
        assert [section.chapter for section in sections] == ['Introduction', 'Registers']
        assert sections[0].key == first.get_children()[0].get_children()[0].get_children()[0].location
        assert all(section.hours_first for section in sections)

    def test_a_subsection_graded_as_timing_feedback_is_found_whatever_its_name(self):
        sections = timing_feedback.find_timing_sections(
            course_of(('Chapter', [timing_sequential('Reflection', graded_format='TimingFeedback')]))
        )
        assert [section.label for section in sections] == ['Reflection']

    def test_a_timing_name_without_a_timing_problem_is_not_a_section(self):
        course = course_of(('Chapter', [
            timing_sequential(
                'Timing Feedback 01: Empty', problem_xml='<problem><label>How are you?</label></problem>'
            ),
            FakeBlock('sequential', 'Timing Feedback 02: Nothing in it', graded_format='TimingFeedback'),
        ]))
        assert not timing_feedback.find_timing_sections(course)

    def test_a_timing_problem_in_an_ordinary_subsection_is_not_a_section(self):
        # Vulns1001 asks how long each in-the-wild vulnerability took, in subsections that are not Timing Feedback
        course = course_of(('Chapter', [timing_sequential('Zephyr RTOS - CVE-2021-3434', graded_format=None)]))
        assert not timing_feedback.find_timing_sections(course)

    def test_minutes_first(self):
        sequential = timing_sequential('Timing Feedback 01: A', problem_xml=MINUTES_FIRST_PROBLEM_XML)
        course = course_of(('Chapter', [sequential]))
        assert timing_feedback.find_timing_sections(course)[0].hours_first is False

    def test_subsections_named_alike_are_labeled_with_their_chapters(self):
        course = course_of(
            ('Download & Install', [timing_sequential('Beta-tester Time Collection')]),
            ('Create Test Executable', [timing_sequential('Beta-tester Time Collection')]),
        )
        sections = timing_feedback.find_timing_sections(course)
        assert [section.label for section in sections] == ['Download & Install', 'Create Test Executable']

    def test_what_is_still_repeated_is_numbered(self):
        course = course_of(
            ('Setup', [timing_sequential('Beta-tester Time Collection') for _ in range(2)]),
            ('Setup', [timing_sequential('Beta-tester Time Collection')]),
        )
        sections = timing_feedback.find_timing_sections(course)
        assert [section.label for section in sections] == ['Setup', 'Setup (2)', 'Setup (3)']
        assert len({section.key for section in sections}) == 3

    def test_two_problems_in_one_subsection_are_two_sections(self):
        course = course_of(('Chapter', [timing_sequential('Timing Feedback 01: A', problems=2)]))
        assert [section.label for section in timing_feedback.find_timing_sections(course)] == ['01: A', '01: A (2)']

    def test_a_course_without_timing_feedback(self):
        assert not timing_feedback.find_timing_sections(course_of(('Chapter', [FakeBlock('sequential', 'Lesson')])))
        assert not timing_feedback.find_timing_sections(course_of())


@ddt.ddt
class ExtractTimeAnswersTest(unittest.TestCase):
    """
    Tests for extract_time_answers.
    """
    def test_in_the_order_of_the_input_ids(self):
        assert timing_feedback.extract_time_answers({'abc_2_1': '01', 'abc_3_1': '30'}) == ('01', '30')

    def test_the_stored_order_does_not_matter(self):
        # Half of the answers in the real databases are stored with the minutes first.
        assert timing_feedback.extract_time_answers({'abc_3_1': '30', 'abc_2_1': '01'}) == ('01', '30')

    def test_input_numbers_are_numbers_not_text(self):
        student_answers = {'abc_10_1': 'second', 'abc_9_1': 'first'}
        assert timing_feedback.extract_time_answers(student_answers) == ('first', 'second')

    def test_minutes_first_problem(self):
        student_answers = {'abc_2_1': '30', 'abc_3_1': '01'}
        assert timing_feedback.extract_time_answers(student_answers, hours_first=False) == ('01', '30')

    def test_ids_that_are_not_input_ids(self):
        assert timing_feedback.extract_time_answers({'bb': '2', 'a': '1', 'ccc': '3'}) == ('1', '2')

    @ddt.data(None, {}, {'abc_2_1': '01'}, [], 'abc')
    def test_fewer_than_two_answers(self, student_answers):
        assert timing_feedback.extract_time_answers(student_answers) is None


@ddt.ddt
class ToMinutesTest(unittest.TestCase):
    """
    Tests for to_minutes.
    """
    @ddt.data(
        (('01', '30'), 90),
        (('00', '00'), 0),
        (('12', '59'), 779),
        ((' 2 ', '05'), 125),
        ((3, 15), 195),
    )
    @ddt.unpack
    def test_minutes(self, answers, expected):
        assert timing_feedback.to_minutes(*answers) == expected

    @ddt.data(
        ('01', 'option_5f4a6020fc53f4885942_3_1_dummy_default'),
        ('option_5f4a6020fc53f4885942_2_1_dummy_default', '37'),
        ('1.5', '00'),
        ('', '30'),
        (None, '30'),
        ('two', 'thirty'),
    )
    @ddt.unpack
    def test_not_whole_numbers(self, hours, minutes):
        assert timing_feedback.to_minutes(hours, minutes) is None


def timing_answers(hours, minutes, section='s', hours_first_in_storage=True):
    """
    The ``student_answers`` of one attempt at a timing problem.
    """
    pairs = [(f'{section}_2_1', hours), (f'{section}_3_1', minutes)]
    return dict(pairs if hours_first_in_storage else reversed(pairs))


class BuildReportTest(unittest.TestCase):
    """
    Tests for build_report.

    The expected numbers are worked out by hand, following the rules of TimingStatsCalculator_v3.py (the script
    behind oneshot.sh and oneshot-i.sh).
    """
    def setUp(self):
        super().setUp()
        self.sections = [
            TimingSection('A', 'Section A', 'Timing Feedback A', 'Chapter A'),
            TimingSection('B', 'Section B', 'Timing Feedback B', 'Chapter B'),
            TimingSection('C', 'Section C', 'Timing Feedback C', 'Chapter C'),
        ]
        self.rows = [
            ('alice', 'A', timing_answers('01', '00', 'A')), ('alice', 'B', timing_answers('00', '30', 'B')),
            ('alice', 'C', timing_answers('01', '30', 'C')),
            ('bob', 'A', timing_answers('02', '00', 'A')), ('bob', 'B', timing_answers('01', '00', 'B')),
            ('bob', 'C', timing_answers('00', '30', 'C')),
            ('carol', 'A', timing_answers('00', '30', 'A')), ('carol', 'B', timing_answers('00', '15', 'B')),
            ('dave', 'A', timing_answers('05', '00', 'A')),
            # an hours dropdown that was left alone, then a good answer
            ('erin', 'A', timing_answers('01', 'option_A_3_1_dummy_default', 'A')),
            ('erin', 'B', timing_answers('00', '45', 'B')),
            # both dropdowns empty, which the script skips, and an attempt that is not one of the sections
            ('frank', 'A', timing_answers('', '', 'A')), ('frank', 'Z', timing_answers('01', '00', 'Z')),
        ]

    def test_all_responses(self):
        view = timing_feedback.build_report(self.sections, self.rows)['views']['all']

        assert view['learners'] == 5
        assert view['average_total'] == 156  # (180 + 210 + 45 + 300 + 45) / 5
        assert view['median_total'] == 180  # 45 45 [180] 210 300
        assert view['sum_of_averages'] == 225.0  # 127.5 + 37.5 + 60
        assert view['sum_of_medians'] == 187.5  # 90 + 37.5 + 60
        assert [
            (row['label'], row['count'], row['average'], row['median'], row['min'], row['max'])
            for row in view['sections']
        ] == [
            ('Section A', 4, 127.5, 90, 30, 300),  # alice 60, bob 120, carol 30, dave 300
            ('Section B', 4, 37.5, 37.5, 15, 60),  # alice 30, bob 60, carol 15, erin 45
            ('Section C', 2, 60, 60, 30, 90),  # alice 90, bob 30
        ]
        assert view['learner_totals'] == [
            {'username': 'carol', 'sections': 2, 'total': 45, 'excluded': False},
            {'username': 'erin', 'sections': 1, 'total': 45, 'excluded': False},
            {'username': 'alice', 'sections': 3, 'total': 180, 'excluded': False},
            {'username': 'bob', 'sections': 3, 'total': 210, 'excluded': False},
            {'username': 'dave', 'sections': 1, 'total': 300, 'excluded': False},
        ]

    def test_complete_responses_only(self):
        view = timing_feedback.build_report(self.sections, self.rows)['views']['complete']

        assert view['learners'] == 2  # alice and bob
        assert view['average_total'] == 195
        assert view['median_total'] == 195
        assert view['sum_of_averages'] == 195.0  # 90 + 45 + 60: the same learners answered every section
        assert view['sum_of_medians'] == 195.0
        assert [(row['count'], row['average'], row['min'], row['max']) for row in view['sections']] == [
            (2, 90, 60, 120), (2, 45, 30, 60), (2, 60, 30, 90),
        ]
        assert [row['username'] for row in view['learner_totals']] == ['alice', 'bob']

    def test_answers_that_are_not_whole_numbers_are_ignored_and_counted(self):
        report = timing_feedback.build_report(self.sections, self.rows)
        assert report['invalid_responses'] == 1  # erin's hours
        erin = {'username': 'erin', 'sections': 1, 'total': 45, 'excluded': False}
        assert erin in report['views']['all']['learner_totals']

    def test_the_stored_order_of_the_answers_does_not_matter(self):
        rows = [('alice', 'A', timing_answers('01', '30', 'A', hours_first_in_storage=False))]
        view = timing_feedback.build_report(self.sections, rows)['views']['all']
        assert view['learner_totals'] == [{'username': 'alice', 'sections': 1, 'total': 90, 'excluded': False}]

    def test_a_problem_that_asks_for_minutes_first(self):
        sections = [TimingSection('A', 'Section A', 'Timing Feedback A', 'Chapter A', hours_first=False)]
        rows = [('alice', 'A', {'A_2_1': '30', 'A_3_1': '01'})]
        view = timing_feedback.build_report(sections, rows)['views']['all']
        assert view['learner_totals'] == [{'username': 'alice', 'sections': 1, 'total': 90, 'excluded': False}]

    def test_sections_that_nobody_answered_do_not_count_against_complete_responses(self):
        sections = self.sections + [TimingSection('D', 'Section D', 'Timing Feedback D', 'Chapter D')]
        report = timing_feedback.build_report(sections, self.rows)
        assert [section['has_responses'] for section in report['sections']] == [True, True, True, False]
        assert report['views']['complete']['learners'] == 2
        assert report['views']['complete']['sections'][3] == {
            'id': 'D', 'label': 'Section D', 'count': 0, 'average': None, 'median': None, 'min': None, 'max': None,
        }

    def test_a_section_with_only_empty_answers_still_counts_as_answered(self):
        # As in the script: the learners need the answers to every section that has any attempt with answers
        sections = self.sections + [TimingSection('D', 'Section D', 'Timing Feedback D', 'Chapter D')]
        rows = self.rows + [('gina', 'D', {})]
        report = timing_feedback.build_report(sections, rows)
        assert report['sections'][3]['has_responses']
        assert report['views']['complete']['learners'] == 0
        assert report['views']['complete']['average_total'] is None
        assert report['views']['all']['learners'] == 5

    def test_an_excluded_learner_counts_as_if_their_answers_were_not_there(self):
        without_dave = [row for row in self.rows if row[0] != 'dave']
        excluded = timing_feedback.build_report(self.sections, self.rows, excluded_usernames=['dave'])
        expected = timing_feedback.build_report(self.sections, without_dave)
        for name in ('all', 'complete'):
            excluded_view, expected_view = excluded['views'][name], expected['views'][name]
            assert [learner for learner in excluded_view['learner_totals'] if not learner['excluded']] == (
                expected_view['learner_totals']
            )
            for key in ('learners', 'sum_of_averages', 'sum_of_medians', 'average_total', 'median_total', 'sections'):
                assert excluded_view[key] == expected_view[key], (name, key)
        # section A's average drops from 127.5 to 70: 70 + 37.5 + 60
        assert excluded['views']['all']['sum_of_averages'] == 167.5
        assert excluded['excluded'] == ['dave']

    def test_excluded_learners_are_still_listed_where_they_would_be(self):
        report = timing_feedback.build_report(self.sections, self.rows, excluded_usernames=['dave', 'alice'])
        everyone = report['views']['all']['learner_totals']
        assert {'username': 'dave', 'sections': 1, 'total': 300, 'excluded': True} in everyone
        assert {'username': 'alice', 'sections': 3, 'total': 180, 'excluded': True} in everyone
        # dave did not answer every section, so he is not in the complete view at all; alice is, but not counted
        complete = report['views']['complete']
        assert [(learner['username'], learner['excluded']) for learner in complete['learner_totals']] == [
            ('alice', True), ('bob', False),
        ]
        assert complete['learners'] == 1

    def test_excluded_learners_do_not_make_a_section_answered_or_count_ignored_answers(self):
        sections = self.sections + [TimingSection('D', 'Section D', 'Timing Feedback D', 'Chapter D')]
        rows = self.rows + [('troll', 'D', timing_answers('12', '59', 'D'))]
        report = timing_feedback.build_report(sections, rows, excluded_usernames=['troll', 'erin'])
        assert not report['sections'][3]['has_responses']
        assert report['views']['complete']['learners'] == 2  # alice and bob still answered every answered section
        assert report['invalid_responses'] == 0  # erin's ignored answer is not counted either
        assert report['excluded'] == ['erin', 'troll']

    def test_excluding_someone_who_never_answered(self):
        report = timing_feedback.build_report(self.sections, self.rows, excluded_usernames=['nobody'])
        assert report['excluded'] == []
        assert report['views']['all']['learners'] == 5

    def test_no_sections(self):
        report = timing_feedback.build_report([], [])
        assert report['sections'] == []
        assert report['invalid_responses'] == 0
        for view in report['views'].values():
            assert view['learners'] == 0
            assert view['sections'] == []
            assert view['learner_totals'] == []
            assert view['average_total'] is None and view['sum_of_averages'] is None

    def test_no_answers(self):
        report = timing_feedback.build_report(self.sections, [])
        assert [section['has_responses'] for section in report['sections']] == [False, False, False]
        assert report['views']['all']['learners'] == 0
        assert report['views']['complete']['learners'] == 0

    def test_the_report_can_be_sent_as_json(self):
        import json
        report = timing_feedback.build_report(self.sections, self.rows)
        assert json.loads(json.dumps(report)) == report
