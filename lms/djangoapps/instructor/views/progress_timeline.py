"""
The data behind the "Progress Over Time" tab of the instructor dashboard.
"""
import datetime as dt
import logging
import time
from functools import lru_cache

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Max, Q
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_control
from opaque_keys.edx.django.models import CourseKeyField
from opaque_keys.edx.keys import CourseKey, UsageKey
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from xblock.core import XBlock
from xblock.fields import Field

from common.djangoapps.student.models import CourseAccessRole
from common.djangoapps.util.json_request import JsonResponse
from lms.djangoapps.course_blocks.transformers.visibility import VisibilityTransformer
from lms.djangoapps.courseware.models import StudentModule
from lms.djangoapps.grades.models import (
    PersistentCourseGrade,
    PersistentSubsectionGrade,
    PersistentSubsectionGradeOverride
)
from lms.djangoapps.grades.transformer import GradesTransformer
from lms.djangoapps.instructor import permissions
from lms.djangoapps.instructor.progress_timeline import (
    DAY,
    BadRequest,
    CourseGradeModel,
    GradeHistory,
    build_window,
    parse_params,
    rank_and_shape
)
from openedx.core.djangoapps.content.block_structure.api import get_course_in_cache
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from xmodule.modulestore import ModuleStoreEnum  # lint-amnesty, pylint: disable=wrong-import-order
from xmodule.modulestore.django import modulestore  # lint-amnesty, pylint: disable=wrong-import-order

log = logging.getLogger(__name__)

# When fewer learners were active in the period than were asked for, the chart is filled with the learners who were
# active most recently before it, looked for in these widening windows.  Each window is a range of the `modified`
# index, which keeps MySQL off a scan of the whole course (about 17 s for the largest course on dev, against well
# under a second for 30 days); only the last, unbounded, one can be that slow.
FILL_LOOKBACKS = (30 * DAY, 90 * DAY, 365 * DAY, None)
# Learners per query when reading their scores.
QUERY_CHUNK = 500
# The course's published versions, reduced to what grades depend on, are cached under its published head; a later
# publish only reads the versions since the cached head.
HISTORY_CACHE_KEY = 'instructor.progress_timeline.history.v1.{}'
HISTORY_CACHE_TIMEOUT = 30 * DAY
# A guard against reading an endless history; the oldest version read stands for everything before it.
MAX_VERSIONS = 20000
# Structure documents read from MongoDB at a time.
STRUCTURE_BATCH = 200
# The raw points of a scored block that is no longer in the course and that nobody has a score on (a Done block is
# worth 1, as are most problems).
UNKNOWN_MAX = 1.0


def _utc(timestamp):
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)


def course_grader(course_key):
    """
    The assignment types of the course's grading policy.
    """
    return [
        {
            'type': assignment['type'],
            'min_count': int(assignment.get('min_count', 0)),
            'drop_count': int(assignment.get('drop_count', 0)),
            'weight': float(assignment.get('weight', 0)),
        }
        for assignment in modulestore().get_course(course_key, depth=0).grading_policy['GRADER']
    ]


def current_structure(course_key):
    """
    Return (subsections, blocks), as CourseGradeModel takes them, for the course as published now.

    Reads the course's cached block structure, the same data that the grades app calculates grades from.
    """
    structure = get_course_in_cache(course_key)

    def staff_only(key):
        return bool(structure.get_transformer_block_field(
            key, VisibilityTransformer, VisibilityTransformer.MERGED_VISIBLE_TO_STAFF_ONLY, False
        ))

    subsections, blocks, seen_blocks = [], [], set()
    for chapter in structure.get_children(structure.root_block_usage_key):
        for sequential in structure.get_children(chapter):
            sub = len(subsections)
            subsections.append({
                'key': str(sequential),
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
                if not structure.get_xblock_field(key, 'has_score', False) or str(key) in seen_blocks:
                    continue
                seen_blocks.add(str(key))
                blocks.append({
                    'key': str(key),
                    'sub': sub,
                    'weight': structure.get_xblock_field(key, 'weight'),
                    'max': structure.get_transformer_block_field(key, GradesTransformer, 'max_score'),
                    'graded': bool(structure.get_xblock_field(key, 'graded', False)),
                    'staff_only': staff_only(key),
                })
    return subsections, blocks


def course_grade_model(course_key):
    """
    Return (CourseGradeModel, {usage key string: block index}) for the course as published now.
    """
    subsections, blocks = current_structure(course_key)
    return CourseGradeModel(course_grader(course_key), subsections, blocks), {b['key']: i for i, b in enumerate(blocks)}


@lru_cache(maxsize=None)
def _block_class_info(block_type):
    """
    (has_score, the name of the has_score field or None, the default weight) of an XBlock type, from its class.
    """
    try:
        block_class = XBlock.load_class(block_type)
    except Exception:  # pylint: disable=broad-except
        return False, None, None
    has_score = getattr(block_class, 'has_score', False)
    weight_field = getattr(block_class, 'fields', {}).get('weight')
    weight = weight_field.default if weight_field is not None else None
    if isinstance(has_score, Field):
        return bool(has_score.default), has_score.name, weight
    return has_score is True, None, weight


# The only parts of a split-modulestore structure document that grades depend on (a structure also holds every block's
# edit history, which is most of its size).
_GRADING_FIELDS = ('graded', 'format', 'weight', 'visible_to_staff_only', 'has_score')
STRUCTURE_PROJECTION = dict(
    {'root': 1, 'edited_on': 1, 'previous_version': 1, 'blocks.block_type': 1, 'blocks.block_id': 1,
     'blocks.fields.children': 1},
    **{f'blocks.fields.{name}': 1 for name in _GRADING_FIELDS},
    **{f'blocks.defaults.{name}': 1 for name in _GRADING_FIELDS},
)


def digest_structure(doc, usage_key):
    """
    One version of the course, from its split-modulestore structure document (projected to STRUCTURE_PROJECTION),
    reduced to what its grades depend on:
    ((subsection key, format, graded, staff only, ((block key, weight, graded, staff only), ...)), ...), with only the
    scored blocks, in course order.  `graded` and `visible_to_staff_only` are inherited as the platform inherits them.

    usage_key(block_type, block_id) returns the usage key string of a block.
    """
    blocks = {(block['block_type'], block['block_id']): block for block in doc['blocks']}

    def field(key, name, default):
        block = blocks.get(key)
        if block is None:
            return default
        fields = block.get('fields', {})
        if name in fields:
            return fields[name]
        return block.get('defaults', {}).get(name, default)

    def children(key):
        block = blocks.get(key)
        return [tuple(child) for child in block.get('fields', {}).get('children', [])] if block is not None else []

    def scored(key):
        has_score, has_score_field, _ = _block_class_info(key[0])
        return bool(field(key, has_score_field, has_score)) if has_score_field else has_score

    root = tuple(doc['root'])
    root_graded = field(root, 'graded', False)
    root_staff = bool(field(root, 'visible_to_staff_only', False))
    digest, seen_blocks = [], set()
    for chapter in children(root):
        chapter_graded = field(chapter, 'graded', root_graded)
        chapter_staff = root_staff or bool(field(chapter, 'visible_to_staff_only', False))
        for sequential in children(chapter):
            graded = field(sequential, 'graded', chapter_graded)
            staff = chapter_staff or bool(field(sequential, 'visible_to_staff_only', False))
            scored_blocks = []
            stack, seen = [(child, graded, staff) for child in children(sequential)], set()
            while stack:
                key, parent_graded, parent_staff = stack.pop()
                if key in seen:
                    continue
                seen.add(key)
                block_graded = field(key, 'graded', parent_graded)
                block_staff = parent_staff or bool(field(key, 'visible_to_staff_only', False))
                stack.extend((child, block_graded, block_staff) for child in children(key))
                if key in seen_blocks or not scored(key):
                    continue
                seen_blocks.add(key)
                scored_blocks.append((usage_key(*key), field(key, 'weight', _block_class_info(key[0])[2]),
                                      bool(block_graded), block_staff))
            digest.append((
                usage_key(*sequential), field(sequential, 'format', None) or '', bool(graded), staff,
                tuple(scored_blocks),
            ))
    return tuple(digest)


def published_versions(course_key):
    """
    [(published at, digest)] of the course's published versions, oldest first, where each run of versions that grade
    alike is one entry dated by its first version; None when the course has no readable history.
    """
    store = modulestore()._get_modulestore_for_courselike(course_key)  # pylint: disable=protected-access
    if not hasattr(store, 'get_course_index') or not hasattr(store, 'db_connection'):
        return None
    index = store.get_course_index(course_key)
    head = index and index.get('versions', {}).get(ModuleStoreEnum.BranchName.published)
    if not head:
        return None
    cache_key = HISTORY_CACHE_KEY.format(course_key)
    cached = cache.get(cache_key)
    if cached and cached['head'] == str(head):
        return cached['runs']
    structures = store.db_connection.structures
    stop = cached['head'] if cached else None
    # The chain of versions back to the cached head (or to the start), newest first; then the documents in batches.
    chain, version = [], head
    while version and str(version) != stop and len(chain) < MAX_VERSIONS:
        chain.append(version)
        version = (structures.find_one({'_id': version}, {'previous_version': 1}) or {}).get('previous_version')
    keys = {}

    def usage_key(block_type, block_id):
        key = keys.get((block_type, block_id))
        if key is None:
            key = keys[(block_type, block_id)] = str(course_key.make_usage_key(block_type, block_id))
        return key

    newer, previous = [], None
    for i in range(0, len(chain), STRUCTURE_BATCH):
        batch = chain[i:i + STRUCTURE_BATCH]
        docs = {doc['_id']: doc for doc in structures.find({'_id': {'$in': batch}}, STRUCTURE_PROJECTION)}
        for version_id in batch:
            doc = docs[version_id]
            # Most versions change nothing that grades depend on, which the projected documents show directly.
            if previous is not None and doc['root'] == previous[0]['root'] and doc['blocks'] == previous[0]['blocks']:
                digest = previous[1]
            else:
                digest = digest_structure(doc, usage_key)
            previous = (doc, digest)
            newer.append((doc['edited_on'].timestamp(), digest))
    runs = list(cached['runs']) if cached and version and str(version) == stop else []
    for published_at, digest in reversed(newer):
        if not runs or runs[-1][1] != digest:
            runs.append((published_at, digest))
    cache.set(cache_key, {'head': str(head), 'runs': runs}, HISTORY_CACHE_TIMEOUT)
    return runs


def recorded_maxima(course_key, block_keys):
    """
    {usage key string: the largest raw possible score recorded on the block}, for blocks no longer in the course.
    """
    maxima, keys = {}, sorted(block_keys)
    for i in range(0, len(keys), QUERY_CHUNK):
        chunk = [UsageKey.from_string(key) for key in keys[i:i + QUERY_CHUNK]]
        for usage_key, raw_max in StudentModule.objects.filter(
            course_id=course_key, module_state_key__in=chunk, max_grade__isnull=False
        ).values('module_state_key').annotate(raw_max=Max('max_grade')).values_list('module_state_key', 'raw_max'):
            maxima[str(usage_key)] = raw_max
    return maxima


def course_grade_history(course_key):
    """
    The GradeHistory of the course: a CourseGradeModel for each published version that grades differently from the
    one before it, the current one taken from the cached block structure like the grades app takes it.
    """
    grader = course_grader(course_key)
    subsections, blocks = current_structure(course_key)
    current = CourseGradeModel(grader, subsections, blocks)
    try:
        runs = published_versions(course_key)
    except Exception:  # pylint: disable=broad-except
        log.exception('Progress Over Time: could not read the published versions of %s', course_key)
        runs = None
    if not runs:
        return GradeHistory(grader, [(0.0, current)])
    current_max = {block['key']: block['max'] for block in blocks}
    gone = {
        block[0] for _, digest in runs[:-1] for subsection in digest for block in subsection[4]
        if block[0] not in current_max
    }
    maxima = dict(recorded_maxima(course_key, gone), **current_max)
    snapshots = []
    for published_at, digest in runs[:-1]:
        version_subs, version_blocks, seen = [], [], set()
        for sub, (key, fmt, graded, staff, scored_blocks) in enumerate(digest):
            version_subs.append({'key': key, 'format': fmt, 'graded': graded, 'staff_only': staff})
            for block_key, weight, block_graded, block_staff in scored_blocks:
                if block_key in seen:
                    continue
                seen.add(block_key)
                version_blocks.append({
                    'key': block_key, 'sub': sub, 'weight': weight, 'max': maxima.get(block_key, UNKNOWN_MAX),
                    'graded': block_graded, 'staff_only': block_staff,
                })
        snapshots.append((published_at, CourseGradeModel(grader, version_subs, version_blocks)))
    snapshots.append((runs[-1][0], current))
    return GradeHistory(grader, snapshots)


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


def read_scores(scored, block_keys, user_ids, end):
    """
    Every score of the learners from before the end: {user id: [(block key, earned, possible, modified), ...]}.
    """
    rows_by_user = {user_id: [] for user_id in user_ids}
    for i in range(0, len(user_ids), QUERY_CHUNK):
        rows = scored.filter(modified__lt=_utc(end), student_id__in=user_ids[i:i + QUERY_CHUNK]).values_list(
            'student_id', 'module_state_key', 'grade', 'max_grade', 'modified'
        )
        for user_id, usage_key, grade, max_grade, modified in rows.iterator(chunk_size=5000):
            key = str(usage_key)
            if key in block_keys:
                rows_by_user[user_id].append((key, grade, max_grade, modified.timestamp()))
    return rows_by_user


def read_stored(course_key, user_ids, end):
    """
    The grades edx-platform stored for the learners before the end: {user id: ([(time, subsection key, earned,
    possible), ...], when their course grade was stored or None, that course grade or None)}, with grade overrides
    applied as the grades app applies them.
    """
    stored = {user_id: ([], None, None) for user_id in user_ids}
    before = _utc(end)
    for i in range(0, len(user_ids), QUERY_CHUNK):
        chunk = user_ids[i:i + QUERY_CHUNK]
        overrides = {
            (user_id, str(usage_key)): (earned, possible)
            for user_id, usage_key, earned, possible in PersistentSubsectionGradeOverride.objects.filter(
                grade__course_id=course_key, grade__user_id__in=chunk
            ).values_list('grade__user_id', 'grade__usage_key', 'earned_graded_override', 'possible_graded_override')
        }
        for user_id, usage_key, earned, possible, modified in PersistentSubsectionGrade.objects.filter(
            course_id=course_key, user_id__in=chunk, modified__lt=before
        ).values_list('user_id', 'usage_key', 'earned_graded', 'possible_graded', 'modified').iterator(chunk_size=5000):
            key = str(usage_key)
            override = overrides.get((user_id, key))
            if override:
                earned = earned if override[0] is None else override[0]
                possible = possible if override[1] is None else override[1]
            stored[user_id][0].append((modified.timestamp(), key, earned, possible))
        for user_id, modified, percent in PersistentCourseGrade.objects.filter(
            course_id=course_key, user_id__in=chunk, modified__lt=before
        ).values_list('user_id', 'modified', 'percent_grade'):
            stored[user_id] = (stored[user_id][0], modified.timestamp(), percent)
    return stored


@method_decorator(cache_control(no_cache=True, no_store=True, must_revalidate=True), name='dispatch')
class ProgressTimelineReport(APIView):
    """
    Respond with the scores that the chart of learners' grades over a period is drawn from.

    Query parameters (all optional; see progress_timeline.parse_params):
      period  day | week | month | custom (custom also needs start and end, YYYY-MM-DD)
      step    auto | 1h | 6h | 1d | 7d
      limit   how many learners, 1 to 1000 (default 20)
      rank    total (highest grade at the end first, the default) | gain (biggest rise over the period first)
      changed 1 to leave out learners whose grade is the same at the start and the end
      tz      the browser's IANA time zone, for where days and hours begin
    """
    permission_classes = (IsAuthenticated, permissions.InstructorPermission)
    permission_name = permissions.VIEW_PROGRESS_TIMELINE

    def get(self, request, course_id):
        """
        Return the period, the course's grading model, and the learners with their recorded grades, as JSON.
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
        history = course_grade_history(course_key)
        scored = StudentModule.objects.filter(course_id=course_key, grade__isnull=False)
        last_active, active = choose_learners(scored, start, end, opts, course_team_ids(course_key))
        user_ids = list(last_active)
        usernames = dict(get_user_model().objects.filter(id__in=user_ids).values_list('id', 'username'))
        payload = rank_and_shape(
            history, read_scores(scored, history.block_keys, user_ids, end), usernames, window, opts, last_active,
            read_stored(course_key, user_ids, end),
        )
        payload['course'] = {'id': str(course_key), 'data_start': data_start, 'now': now}
        payload['active_in_window'] = active
        return JsonResponse(payload)
