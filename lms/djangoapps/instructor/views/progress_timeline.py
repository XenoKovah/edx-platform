"""
The data behind the "Progress Over Time" tab of the instructor dashboard.
"""
import datetime as dt
import time

from django.contrib.auth import get_user_model
from django.db.models import Max, Q
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_control
from opaque_keys.edx.django.models import CourseKeyField
from opaque_keys.edx.keys import CourseKey
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from common.djangoapps.student.models import CourseAccessRole
from common.djangoapps.util.json_request import JsonResponse
from lms.djangoapps.course_blocks.transformers.visibility import VisibilityTransformer
from lms.djangoapps.courseware.models import StudentModule
from lms.djangoapps.grades.transformer import GradesTransformer
from lms.djangoapps.instructor import permissions
from lms.djangoapps.instructor.progress_timeline import (
    DAY,
    BadRequest,
    CourseGradeModel,
    build_window,
    parse_params,
    rank_and_shape
)
from openedx.core.djangoapps.content.block_structure.api import get_course_in_cache
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from xmodule.modulestore.django import modulestore  # lint-amnesty, pylint: disable=wrong-import-order

# When fewer learners were active in the period than were asked for, the chart is filled with the learners who were
# active most recently before it, looked for in these widening windows.  Each window is a range of the `modified`
# index, which keeps MySQL off a scan of the whole course (about 17 s for the largest course on dev, against well
# under a second for 30 days); only the last, unbounded, one can be that slow.
FILL_LOOKBACKS = (30 * DAY, 90 * DAY, 365 * DAY, None)
# Learners per query when reading their scores.
QUERY_CHUNK = 500


def _utc(timestamp):
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)


def course_grade_model(course_key):
    """
    Return (CourseGradeModel, {usage key string: block index}) for the course as published.

    Reads the course's cached block structure, the same data that the grades app calculates grades from.
    """
    grader = [
        {
            'type': assignment['type'],
            'min_count': int(assignment.get('min_count', 0)),
            'drop_count': int(assignment.get('drop_count', 0)),
            'weight': float(assignment.get('weight', 0)),
        }
        for assignment in modulestore().get_course(course_key, depth=0).grading_policy['GRADER']
    ]
    structure = get_course_in_cache(course_key)

    def staff_only(key):
        return bool(structure.get_transformer_block_field(
            key, VisibilityTransformer, VisibilityTransformer.MERGED_VISIBLE_TO_STAFF_ONLY, False
        ))

    subsections, blocks, block_index = [], [], {}
    for chapter in structure.get_children(structure.root_block_usage_key):
        for sequential in structure.get_children(chapter):
            sub = len(subsections)
            subsections.append({
                'format': structure.get_xblock_field(sequential, 'format') or '',
                'graded': bool(structure.get_xblock_field(sequential, 'graded', False)),
                'staff_only': staff_only(sequential),
            })
            stack, seen = list(structure.get_children(sequential)), set()
            while stack:
                key = stack.pop()
                if key in seen:
                    continue
                seen.add(key)
                stack.extend(structure.get_children(key))
                if not structure.get_xblock_field(key, 'has_score', False) or str(key) in block_index:
                    continue
                block_index[str(key)] = len(blocks)
                blocks.append({
                    'sub': sub,
                    'weight': structure.get_xblock_field(key, 'weight'),
                    'max': structure.get_transformer_block_field(key, GradesTransformer, 'max_score'),
                    'graded': bool(structure.get_xblock_field(key, 'graded', False)),
                    'staff_only': staff_only(key),
                })
    return CourseGradeModel(grader, subsections, blocks), block_index


def course_team_ids(course_key):
    """
    The ids of the users who are left off the chart: global staff, and the course's (or its organization's) team.

    Beta testers stay on it.
    """
    team = set(get_user_model().objects.filter(Q(is_staff=True) | Q(is_superuser=True)).values_list('id', flat=True))
    team.update(
        CourseAccessRole.objects.filter(
            Q(course_id=course_key) | Q(course_id=CourseKeyField.Empty, org__iexact=course_key.org)
        ).exclude(role='beta_testers').values_list('user_id', flat=True)
    )
    return team


def choose_learners(scored, start, end, opts, excluded):
    """
    Return ({user id: last scored activity before the end}, number active in the period) for the learners to rank.

    Everyone with a score change in the period is a candidate.  If that's fewer than were asked for, the most recently
    active learners before the period are added (unless only learners whose grade changed are wanted).
    """
    last_active = {}
    for user_id, last in scored.filter(modified__gte=_utc(start), modified__lt=_utc(end)).values(
        'student_id'
    ).annotate(last=Max('modified')).values_list('student_id', 'last'):
        if user_id not in excluded:
            last_active[user_id] = last.timestamp()
    active = len(last_active)
    need = opts['limit'] - active
    upper = start
    for lookback in FILL_LOOKBACKS:
        if need <= 0 or opts['changed_only']:
            break
        lower = start - lookback if lookback else None
        earlier = scored.filter(modified__lt=_utc(upper))
        if lower is not None:
            earlier = earlier.filter(modified__gte=_utc(lower))
        found = earlier.values('student_id').annotate(last=Max('modified')).order_by('-last').values_list(
            'student_id', 'last'
        )[:need + len(excluded) + len(last_active)]
        for user_id, last in found:
            if user_id in excluded or user_id in last_active:
                continue
            last_active[user_id] = last.timestamp()
            need -= 1
            if need <= 0:
                break
        upper = lower
    return last_active, active


def read_scores(scored, block_index, user_ids, end):
    """
    Every score of the learners from before the end: {user id: [(block index, earned, possible, modified), ...]}.
    """
    rows_by_user = {user_id: [] for user_id in user_ids}
    for i in range(0, len(user_ids), QUERY_CHUNK):
        rows = scored.filter(modified__lt=_utc(end), student_id__in=user_ids[i:i + QUERY_CHUNK]).values_list(
            'student_id', 'module_state_key', 'grade', 'max_grade', 'modified'
        )
        for user_id, usage_key, grade, max_grade, modified in rows.iterator(chunk_size=5000):
            index = block_index.get(str(usage_key))
            if index is not None:
                rows_by_user[user_id].append((index, grade, max_grade, modified.timestamp()))
    return rows_by_user


@method_decorator(cache_control(no_cache=True, no_store=True, must_revalidate=True), name='dispatch')
class ProgressTimelineReport(APIView):
    """
    Respond with the scores that the chart of learners' grades over a period is drawn from.

    Query parameters (all optional; see progress_timeline.parse_params):
      period  day | week | month | custom (custom also needs start and end, YYYY-MM-DD)
      step    auto | 1h | 6h | 1d | 7d
      limit   how many learners, 1 to 1000 (default 20)
      rank    gain (most progress in the period first) | recent (most recently active first)
      changed 1 to leave out learners whose grade is the same at the start and the end
      tz      the browser's IANA time zone, for where days and hours begin
    """
    permission_classes = (IsAuthenticated, permissions.InstructorPermission)
    permission_name = permissions.VIEW_PROGRESS_TIMELINE

    def get(self, request, course_id):
        """
        Return the period, the course's grading model, and the learners with their scores, as JSON.
        """
        course_key = CourseKey.from_string(course_id)
        now = time.time()
        course_start = CourseOverview.get_from_id(course_key).start
        data_start = course_start.timestamp() if course_start and course_start.timestamp() < now else None
        try:
            opts = parse_params(request.GET)
            window = build_window(opts, now, data_start)
        except BadRequest as exc:
            return JsonResponse({'error': str(exc)}, status=400)
        start, end = window[0], window[1]
        model, block_index = course_grade_model(course_key)
        scored = StudentModule.objects.filter(course_id=course_key, grade__isnull=False)
        last_active, active = choose_learners(scored, start, end, opts, course_team_ids(course_key))
        user_ids = list(last_active)
        usernames = dict(get_user_model().objects.filter(id__in=user_ids).values_list('id', 'username'))
        payload = rank_and_shape(
            model, read_scores(scored, block_index, user_ids, end), usernames, window, opts, last_active
        )
        payload['course'] = {'id': str(course_key), 'data_start': data_start, 'now': now}
        payload['active_in_window'] = active
        return JsonResponse(payload)
