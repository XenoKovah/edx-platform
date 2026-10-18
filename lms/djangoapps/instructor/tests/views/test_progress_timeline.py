"""
Tests for the "Progress Over Time" tab of the instructor dashboard, and for the data behind it.
"""
import datetime as dt

from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone
from pyquery import PyQuery as pq

from common.djangoapps.student.tests.factories import (
    AdminFactory,
    CourseAccessRoleFactory,
    InstructorFactory,
    StaffFactory,
    UserFactory
)
from lms.djangoapps.courseware.models import StudentModule
from lms.djangoapps.courseware.tests.factories import StudentModuleFactory
from lms.djangoapps.courseware.tests.helpers import LoginEnrollmentTestCase
from lms.djangoapps.instructor.views.progress_timeline import course_grade_model
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import BlockFactory, CourseFactory

PROBLEM_XML = """<problem>
  <multiplechoiceresponse>
    <choicegroup type="MultipleChoice">
      <choice correct="true">Yes</choice>
      <choice correct="false">No</choice>
    </choicegroup>
  </multiplechoiceresponse>
</problem>"""

GRADING_POLICY = {
    'GRADER': [
        {'type': 'Homework', 'min_count': 2, 'drop_count': 0, 'short_label': 'HW', 'weight': 1.0},
    ],
    'GRADE_CUTOFFS': {'Pass': 0.5},
}


class ProgressTimelineTestCase(ModuleStoreTestCase, LoginEnrollmentTestCase):
    """
    A course with two graded homework subsections of one problem each, and an ungraded lesson, built in 2020.
    """
    def setUp(self):
        super().setUp()
        self.course = CourseFactory.create(
            display_name='Progress course', start=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        )
        self.chapter = BlockFactory.create(parent=self.course, category='chapter', display_name='Week 1')
        self.problems = [self.add_homework(number) for number in (1, 2)]
        lesson = BlockFactory.create(parent=self.chapter, category='sequential', display_name='Lesson')
        lesson_unit = BlockFactory.create(parent=lesson, category='vertical', display_name='Lesson unit')
        self.practice = BlockFactory.create(
            parent=lesson_unit, category='problem', display_name='Practice', data=PROBLEM_XML
        )
        self.course = self.store.get_course(self.course.id)
        self.course.set_grading_policy(GRADING_POLICY)
        self.store.update_item(self.course, 0)
        self.date_published_versions(dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
        self.url = reverse('progress_timeline', kwargs={'course_id': str(self.course.id)})
        self.dashboard_url = reverse('instructor_dashboard', kwargs={'course_id': str(self.course.id)})

    def add_homework(self, number):
        """
        Add a graded homework subsection with one problem to the course, and return the problem.
        """
        sequential = BlockFactory.create(
            parent=self.chapter, category='sequential', display_name=f'Homework {number}',
            metadata={'graded': True, 'format': 'Homework'},
        )
        vertical = BlockFactory.create(parent=sequential, category='vertical', display_name=f'Unit {number}')
        return BlockFactory.create(
            parent=vertical, category='problem', display_name=f'Question {number}', data=PROBLEM_XML
        )

    def date_published_versions(self, when):
        """
        Date every version of the course published so far at the given time, as if it had been built then.
        """
        store = self.store._get_modulestore_for_courselike(self.course.id)  # pylint: disable=protected-access
        store.db_connection.structures.update_many({}, {'$set': {'edited_on': when}})

    def score(self, user, problem, ago, grade=1):
        """
        Record that the learner scored grade out of 1 on the problem, the given timedelta ago.
        """
        row = StudentModuleFactory.create(
            student=user, course_id=self.course.id, module_state_key=problem.location, grade=grade, max_grade=1
        )
        # `modified` is set on every save, so it is changed behind the model's back.
        StudentModule.objects.filter(pk=row.pk).update(modified=timezone.now() - ago)

    def log_in_as(self, user):
        """
        Log in as the user.
        """
        self.client.login(username=user.username, password=self.TEST_PASSWORD)


class ProgressTimelineReportTest(ProgressTimelineTestCase):
    """
    Tests for the data that the chart is drawn from.
    """
    def setUp(self):
        super().setUp()
        self.staff = StaffFactory.create(course_key=self.course.id)
        self.log_in_as(self.staff)
        self.ann = UserFactory.create(username='ann')
        self.bob = UserFactory.create(username='bob')
        self.cy = UserFactory.create(username='cy')
        hour, day = dt.timedelta(hours=1), dt.timedelta(days=1)
        self.score(self.ann, self.problems[0], 2 * day)
        self.score(self.ann, self.problems[1], hour)
        self.score(self.bob, self.problems[0], 3 * hour)
        self.score(self.bob, self.practice, 2 * hour)
        self.score(self.cy, self.problems[0], 40 * day)
        # The course team is never on the chart.
        self.score(self.staff, self.problems[0], hour)

    def get(self, **params):
        """
        Ask for the data, and check that it came.
        """
        response = self.client.get(self.url, dict({'tz': 'UTC'}, **params))
        assert response.status_code == 200, response.content
        assert response['Cache-Control'].startswith('no-cache')
        return response.json()

    def test_last_day(self):
        payload = self.get(period='day')

        assert [(u['username'], u['pct_start'], u['pct_end']) for u in payload['users']] == [
            ('ann', 50, 100), ('bob', 0, 50), ('cy', 50, 50)
        ]
        assert payload['active_in_window'] == 2
        assert payload['changed_in_window'] == 2
        assert payload['window']['step'] == 3600
        assert len(payload['window']['boundaries']) in (25, 26)
        assert payload['model'] == {'types': [[2, 0, 1.0]], 'subs': 2, 'versions': [[[0, 0, 1.0], [1, 0, 1.0]]]}
        assert payload['course']['id'] == str(self.course.id)
        assert payload['course']['data_start'] == self.course.start.timestamp()
        ann = payload['users'][0]
        assert ann['base'] == {'version': 0, 'pin': None, 'subs': [[0, 1.0, 1.0]]}
        assert [(sub, earned, possible) for _, _, sub, earned, possible in ann['events']] == [(1, 1.0, 1.0)]
        # The ungraded practice problem is not sent.
        assert [(sub, earned) for _, _, sub, earned, _ in payload['users'][1]['events']] == [(0, 1.0)]

    def test_options(self):
        assert [u['username'] for u in self.get(changed='1')['users']] == ['ann', 'bob']
        assert [u['username'] for u in self.get(limit='1')['users']] == ['ann']
        assert [u['username'] for u in self.get(rank='recent')['users']] == ['ann', 'bob', 'cy']
        assert [u['username'] for u in self.get(period='week')['users']] == ['ann', 'bob', 'cy']

    def test_custom_range(self):
        today = timezone.now().date()
        payload = self.get(period='custom', start=str(today - dt.timedelta(days=60)), end=str(today), step='1d')

        assert payload['window']['step'] == 86400
        assert [(u['username'], u['pct_start'], u['pct_end']) for u in payload['users']] == [
            ('ann', 0, 100), ('bob', 0, 50), ('cy', 0, 50)
        ]

    def test_bad_requests_are_refused_with_a_reason(self):
        for params in [
            {'limit': '1001'}, {'limit': '0'}, {'limit': 'lots'}, {'period': 'year'}, {'step': '5m'},
            {'period': 'custom', 'start': '2026-01-02', 'end': '2026-01-01'},
            {'period': 'custom', 'start': '2010-01-01', 'end': '2010-02-01'},
        ]:
            response = self.client.get(self.url, params)
            assert response.status_code == 400, params
            assert response.json()['error'], params

    def test_grades_stay_as_they_were_recorded_when_the_course_grows(self):
        # A third homework is published now: ann finished the first two earlier, and stays at 100% (her Progress page,
        # which recalculates over today's course, would show 67%); dan, who starts now, has one of three.
        third = self.add_homework(3)
        dan = UserFactory.create(username='dan')
        self.score(dan, third, dt.timedelta(0))

        payload = self.get(period='day')

        assert [(u['username'], u['pct_start'], u['pct_end']) for u in payload['users']] == [
            ('ann', 50, 100), ('bob', 0, 50), ('dan', 0, 33), ('cy', 50, 50)
        ]
        assert len(payload['model']['versions']) == 2

    def test_course_without_scores(self):
        StudentModule.objects.all().delete()

        payload = self.get(period='day')

        assert payload['users'] == []
        assert payload['active_in_window'] == 0

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

    def test_beta_testers_stay_on_the_chart(self):
        CourseAccessRoleFactory(course_id=self.course.id, user=self.cy, role='beta_testers', org=self.course.id.org)
        CourseAccessRoleFactory(course_id=self.course.id, user=self.bob, role='instructor', org=self.course.id.org)

        assert [u['username'] for u in self.get()['users']] == ['ann', 'cy']


class CourseGradeModelTest(ProgressTimelineTestCase):
    """
    The grading model is read from the course as published.
    """
    def test_model(self):
        model, block_index = course_grade_model(self.course.id)

        assert set(block_index) == {str(p.location) for p in self.problems + [self.practice]}
        assert model.sub_type == [0, 0, None]
        assert model.sub_possible == [1.0, 1.0, 0.0]
        assert model.percent([1.0, 0.0, 0.0]) == 0.5


@override_settings(PROFILE_MICROFRONTEND_URL='https://apps.example.com/profile/u')
class ProgressTimelineTabTest(ProgressTimelineTestCase):
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
            sections = self.sections_of(user)
            assert sections.index('progress_timeline') == sections.index('completion_time') + 1, user

    def test_data_researchers_who_are_not_staff_do_not(self):
        researcher = UserFactory.create()
        CourseAccessRoleFactory(
            course_id=self.course.id, user=researcher, role='data_researcher', org=self.course.id.org
        )

        assert 'progress_timeline' not in self.sections_of(researcher)

    def test_the_tab_has_what_it_needs(self):
        self.log_in_as(StaffFactory.create(course_key=self.course.id))

        container = pq(self.client.get(self.dashboard_url).content)('#progress_timeline .progress-timeline-container')

        assert container.attr('data-endpoint') == self.url
        assert container.attr('data-profile-url') == 'https://apps.example.com/profile/u/'
        assert container.attr('data-wasm-url').endswith('.wasm')
        assert container.find('.pt-limit').attr('max') == '1000'
        assert container.find('.pt-limit').attr('value') == '20'
        assert [b.attrib['data-period'] for b in container.find('.pt-period-btn')] == ['day', 'week', 'month', 'custom']
        assert container.find('.pt-period-btn[aria-pressed="true"]').attr('data-period') == 'month'
        assert container.find('.pt-changed').attr('checked') is not None
        assert container.find('canvas.pt-lines')
