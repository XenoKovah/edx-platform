"""
Tests for the "Completion Time" tab of the instructor dashboard, and for the data behind it.
"""
import json

from django.urls import reverse
from pyquery import PyQuery as pq

from common.djangoapps.student.tests.factories import (
    AdminFactory,
    CourseAccessRoleFactory,
    InstructorFactory,
    StaffFactory,
    UserFactory
)
from lms.djangoapps.certificates.models import CertificateGenerationConfiguration
from lms.djangoapps.courseware.tests.factories import StudentModuleFactory
from lms.djangoapps.courseware.tests.helpers import LoginEnrollmentTestCase
from lms.djangoapps.instructor.tests.test_timing_feedback import TIMING_PROBLEM_XML
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import BlockFactory, CourseFactory


class CompletionTimeTestCase(ModuleStoreTestCase, LoginEnrollmentTestCase):
    """
    A course with two Timing Feedback sections, and a lesson that has nothing to do with timing.
    """
    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create(display_name='Timing course')
        self.problems = []
        for number, chapter_name in enumerate(['Introduction', 'Registers'], start=1):
            chapter = BlockFactory.create(
                parent=self.course, category='chapter', display_name=chapter_name, publish_item=True
            )
            BlockFactory.create(
                parent=chapter, category='sequential', display_name=f'Lesson {number}', publish_item=True,
                metadata={'graded': True, 'format': 'ProgressMarker'},
            )
            sequential = BlockFactory.create(
                parent=chapter, category='sequential', display_name=f'Timing Feedback 0{number}: {chapter_name}',
                publish_item=True, metadata={'graded': True, 'format': 'TimingFeedback'},
            )
            vertical = BlockFactory.create(
                parent=sequential, category='vertical', display_name=f'Timing Feedback unit {number}', publish_item=True
            )
            problem_parent = vertical
            if number == 2:
                # The courses get the problem from the "timing_feedback" content library, so it is in a block like this
                problem_parent = BlockFactory.create(
                    parent=vertical, category='library_content', display_name='timing_feedback', publish_item=True
                )
            self.problems.append(BlockFactory.create(
                parent=problem_parent, category='problem', display_name=f'How long {number}',
                data=TIMING_PROBLEM_XML, publish_item=True,
            ))

        self.url = reverse('completion_time', kwargs={'course_id': str(self.course.id)})
        self.dashboard_url = reverse('instructor_dashboard', kwargs={'course_id': str(self.course.id)})

    def answer(self, user, problem, hours, minutes, course_id=None):
        """
        Record the learner's answer to a Timing Feedback problem, the way that the LMS stores it.
        """
        block_id = problem.location.block_id
        return StudentModuleFactory.create(
            student=user,
            course_id=course_id or self.course.id,
            module_state_key=problem.location,
            # The LMS stores the answers in no particular order.
            state=json.dumps({
                'seed': 1,
                'student_answers': {f'{block_id}_3_1': minutes, f'{block_id}_2_1': hours},
            }),
        )

    def log_in_as(self, user):
        """
        Log in as the user.
        """
        self.client.login(username=user.username, password=self.TEST_PASSWORD)


class CompletionTimeReportTest(CompletionTimeTestCase):
    """
    Tests for the report that the tab shows.
    """
    def setUp(self):
        super().setUp()
        self.staff = StaffFactory.create(course_key=self.course.id)
        self.log_in_as(self.staff)

    def test_report(self):
        alice = UserFactory.create(username='alice')
        bob = UserFactory.create(username='bob')
        carol = UserFactory.create()
        self.answer(alice, self.problems[0], '01', '30')
        self.answer(alice, self.problems[1], '02', '00')
        self.answer(bob, self.problems[0], '00', '45')
        # carol only looked at the problem
        StudentModuleFactory.create(
            student=carol, course_id=self.course.id, module_state_key=self.problems[0].location,
            state=json.dumps({'seed': 1}),
        )

        response = self.client.get(self.url)

        assert response.status_code == 200
        report = response.json()
        assert report['course_id'] == str(self.course.id)
        assert report['generated_at']
        assert report['invalid_responses'] == 0
        assert [(s['label'], s['sequential'], s['chapter'], s['has_responses']) for s in report['sections']] == [
            ('01: Introduction', 'Timing Feedback 01: Introduction', 'Introduction', True),
            ('02: Registers', 'Timing Feedback 02: Registers', 'Registers', True),
        ]
        assert [s['id'] for s in report['sections']] == [str(problem.location) for problem in self.problems]

        everyone = report['views']['all']
        assert everyone['learners'] == 2
        assert everyone['learner_totals'] == [
            {'username': 'bob', 'sections': 1, 'total': 45, 'excluded': False},
            {'username': 'alice', 'sections': 2, 'total': 210, 'excluded': False},
        ]
        assert [(s['count'], s['average']) for s in everyone['sections']] == [(2, 67.5), (1, 120)]
        assert everyone['sum_of_averages'] == 187.5

        complete = report['views']['complete']
        assert complete['learners'] == 1
        assert complete['learner_totals'] == [{'username': 'alice', 'sections': 2, 'total': 210, 'excluded': False}]
        assert complete['sum_of_averages'] == 210

    def test_excluded_learners_are_left_out(self):
        alice, bob, carol = (UserFactory.create(username=name) for name in ('alice', 'bob', 'carol'))
        self.answer(alice, self.problems[0], '01', '00')
        self.answer(bob, self.problems[0], '00', '30')
        self.answer(carol, self.problems[0], '12', '59')

        report = self.client.get(self.url, {'exclude': ['carol', ' bob ', '', 'someone-else']}).json()

        assert report['excluded'] == ['bob', 'carol']
        everyone = report['views']['all']
        assert everyone['learners'] == 1
        assert everyone['sections'][0]['average'] == 60
        assert [(learner['username'], learner['excluded']) for learner in everyone['learner_totals']] == [
            ('bob', True), ('alice', False), ('carol', True),
        ]

    def test_answers_to_the_same_problem_in_another_course_are_not_counted(self):
        other_course = CourseFactory.create()
        self.answer(UserFactory.create(), self.problems[0], '01', '00', course_id=other_course.id)

        report = self.client.get(self.url).json()

        assert report['views']['all']['learners'] == 0
        assert not report['sections'][0]['has_responses']

    def test_a_half_answered_problem_is_counted_as_ignored(self):
        self.answer(UserFactory.create(), self.problems[0], '01', 'option_x_3_1_dummy_default')

        report = self.client.get(self.url).json()

        assert report['invalid_responses'] == 1
        assert report['views']['all']['learners'] == 0

    def test_a_subsection_graded_as_timing_feedback_is_found_whatever_its_name(self):
        chapter = BlockFactory.create(parent=self.course, category='chapter', display_name='Wrap-up', publish_item=True)
        sequential = BlockFactory.create(
            parent=chapter, category='sequential', display_name='How did it go?', publish_item=True,
            metadata={'graded': True, 'format': 'Timing Feedback'},
        )
        vertical = BlockFactory.create(parent=sequential, category='vertical', display_name='Unit', publish_item=True)
        problem = BlockFactory.create(
            parent=vertical, category='problem', display_name='How long', data=TIMING_PROBLEM_XML, publish_item=True
        )
        self.answer(UserFactory.create(), problem, '00', '20')

        report = self.client.get(self.url).json()

        assert [section['label'] for section in report['sections']][-1] == 'How did it go?'
        assert report['sections'][-1]['id'] == str(problem.location)
        assert report['sections'][-1]['has_responses']

    def test_course_without_timing_feedback(self):
        course = CourseFactory.create()
        BlockFactory.create(parent=course, category='chapter', display_name='Lesson', publish_item=True)
        url = reverse('completion_time', kwargs={'course_id': str(course.id)})
        self.log_in_as(AdminFactory.create())

        report = self.client.get(url).json()

        assert report['sections'] == []
        assert report['views']['all']['learners'] == 0

    def test_who_can_see_it(self):
        for user, status in [
            (InstructorFactory.create(course_key=self.course.id), 200),
            (AdminFactory.create(), 200),
            (UserFactory.create(), 403),
        ]:
            self.log_in_as(user)
            assert self.client.get(self.url).status_code == status, user

    def test_data_researchers_who_are_not_staff_cannot(self):
        researcher = UserFactory.create()
        CourseAccessRoleFactory(
            course_id=self.course.id, user=researcher, role='data_researcher', org=self.course.id.org
        )
        self.log_in_as(researcher)

        assert self.client.get(self.url).status_code == 403

    def test_not_logged_in(self):
        self.client.logout()

        assert self.client.get(self.url).status_code in (401, 403)

    def test_staff_of_another_course_cannot(self):
        other_course = CourseFactory.create()
        self.log_in_as(StaffFactory.create(course_key=other_course.id))

        assert self.client.get(self.url).status_code == 403


class CompletionTimeTabTest(CompletionTimeTestCase):
    """
    Tests for the tab on the instructor dashboard.
    """
    def sections_of(self, user):
        """
        The keys of the tabs that the dashboard shows the user, in order.
        """
        self.log_in_as(user)
        response = self.client.get(self.dashboard_url)
        assert response.status_code == 200
        return [button.attrib['data-section'] for button in pq(response.content)('.instructor-nav button')]

    def test_course_staff_and_instructors_and_global_staff_see_the_tab(self):
        for user in [
            StaffFactory.create(course_key=self.course.id),
            InstructorFactory.create(course_key=self.course.id),
            AdminFactory.create(),
        ]:
            assert 'completion_time' in self.sections_of(user), user

    def test_data_researchers_who_are_not_staff_do_not(self):
        researcher = UserFactory.create()
        CourseAccessRoleFactory(
            course_id=self.course.id, user=researcher, role='data_researcher', org=self.course.id.org
        )

        assert 'completion_time' not in self.sections_of(researcher)

    def test_the_tab_comes_after_certificates(self):
        CertificateGenerationConfiguration.objects.create(enabled=True)

        sections = self.sections_of(AdminFactory.create())

        assert 'certificates' in sections
        assert sections.index('completion_time') == sections.index('certificates') + 1

    def test_the_tab_has_what_it_needs(self):
        self.log_in_as(StaffFactory.create(course_key=self.course.id))

        content = pq(self.client.get(self.dashboard_url).content)

        section = content('#completion_time')
        assert section.find('.completion-time-container').attr('data-endpoint') == self.url
        assert section.find('.completion-time-refresh')
        assert [tab.attrib['data-view'] for tab in section.find('.completion-time-tab')] == ['all', 'complete']
        assert section.find('.completion-time-tab.active-section').attr('data-view') == 'all'
