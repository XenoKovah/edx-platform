"""
The data behind the "Completion Time" tab of the instructor dashboard.
"""
import json

from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_control
from opaque_keys.edx.keys import CourseKey, UsageKey
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from common.djangoapps.util.json_request import JsonResponse
from lms.djangoapps.courseware.models import StudentModule
from lms.djangoapps.instructor import permissions
from lms.djangoapps.instructor.timing_feedback import build_report, find_timing_sections
from openedx.core.lib.courses import get_course_by_id
from xmodule.modulestore.django import modulestore  # lint-amnesty, pylint: disable=wrong-import-order


def _answer_rows(course_key, sections):
    """
    Yield (username, section key, student_answers) for every attempt at a Timing Feedback problem.

    Attempts that were only viewed and never submitted have no ``student_answers`` and are left out.
    """
    if not sections:
        return
    attempts = StudentModule.objects.filter(
        course_id=course_key,
        module_state_key__in=[UsageKey.from_string(section.key) for section in sections],
    ).values_list('student__username', 'module_state_key', 'state')
    for username, usage_key, state in attempts.iterator():
        try:
            student_answers = json.loads(state or '{}').get('student_answers')
        except (AttributeError, ValueError):
            continue
        if student_answers is not None:
            yield username, str(usage_key), student_answers


@method_decorator(cache_control(no_cache=True, no_store=True, must_revalidate=True), name='dispatch')
class CompletionTimeReport(APIView):
    """
    Respond with how long the course's learners say that it takes, from their Timing Feedback answers.

    The Timing Feedback sections are found by looking at the course, so nothing has to be configured.
    The statistics are calculated afresh on every request.
    """
    permission_classes = (IsAuthenticated, permissions.InstructorPermission)
    permission_name = permissions.VIEW_COMPLETION_TIME

    def get(self, request, course_id):  # pylint: disable=unused-argument
        """
        Return the report as JSON.
        """
        course_key = CourseKey.from_string(course_id)
        # The bulk operation keeps the modulestore from reading the course structure again for every block
        # that find_timing_sections walks to.
        with modulestore().bulk_operations(course_key):
            sections = find_timing_sections(get_course_by_id(course_key, depth=2))
        report = build_report(sections, _answer_rows(course_key, sections))
        report['course_id'] = str(course_key)
        report['generated_at'] = timezone.now().isoformat()
        return JsonResponse(report)
