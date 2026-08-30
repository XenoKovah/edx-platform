"""
OST2: shadow-muting of a learner's forum contributions, per course.

A shadow mute hides everything one learner posts in a course from their peers
while leaving it fully visible to the author themselves, so the muted user gets
no feedback that they have been muted and (we hope) stops escalating. Users with
forum moderation privilege keep seeing the content, flagged with
``author_shadow_muted`` so the UI can mark it, which is what makes the content
still reviewable and deletable.

Only the *read* path is filtered. Posting still succeeds, the content is still
written to the forum backend, and the existing "email the moderators" signal
handlers still fire -- an instructor is meant to keep hearing about the posts
they are no longer being bothered by.

The filtering is applied in ``comment_client``, at the handful of functions that
return forum content from the backend, because that is the single choke point
shared by *both* read paths in this deployment: the Discussions MFE
(``lms.djangoapps.discussion.rest_api``) and the legacy inline DiscussionXBlock
rendered under courseware (``lms.djangoapps.discussion.views``).

The viewer is resolved from the current request via ``crum``. When there is no
request -- a Celery task, a management command, a signal handler sending
notification email -- nothing is filtered, which is what keeps the instructor
notifications working.
"""


import logging

from crum import get_current_user
from edx_django_utils.cache import RequestCache
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

log = logging.getLogger(__name__)

REQUEST_CACHE_NAMESPACE = 'django_comment_common.shadow_mute'

# Roles that keep seeing shadow-muted content so they can moderate it. This
# mirrors the roles the discussion REST API treats as having moderation
# privilege, plus the course team, so the instructor who receives the
# new-thread notification email can always open the post it links to.
MODERATION_ROLES = [
    'Administrator',
    'Moderator',
    'Group Moderator',
    'Community TA',
]


def _coerce_course_key(course_id):
    """
    Return a CourseKey for ``course_id``, or None if it cannot be parsed.

    comment_client passes course ids around as both strings and CourseKeys.
    """
    if course_id is None:
        return None
    if isinstance(course_id, CourseKey):
        return course_id
    try:
        return CourseKey.from_string(str(course_id))
    except InvalidKeyError:
        log.warning('Shadow mute: could not parse course id %r', course_id)
        return None


def get_shadow_muted_user_ids(course_id):
    """
    Return the ids of users shadow-muted in this course, as a frozenset of str.

    Forum content carries ``user_id`` as a string, so these are stringified to
    let callers compare without converting on every item. Cached per request:
    a single forum page can serialize hundreds of posts.
    """
    course_key = _coerce_course_key(course_id)
    if course_key is None:
        return frozenset()

    cache = RequestCache(REQUEST_CACHE_NAMESPACE)
    cached = cache.get_cached_response(str(course_key))
    if cached.is_found:
        return cached.value

    # Imported here to keep this module importable from comment_client without
    # dragging in the model layer at import time.
    from openedx.core.djangoapps.django_comment_common.models import ForumShadowMute

    muted = frozenset(
        str(user_id) for user_id in
        ForumShadowMute.objects.filter(
            course_id=course_key, is_active=True
        ).values_list('user_id', flat=True)
    )
    cache.set(str(course_key), muted)
    return muted


def is_shadow_muted(user, course_id):
    """
    Return whether ``user`` (a Django user or a user id) is muted in the course.
    """
    if user is None:
        return False
    user_id = getattr(user, 'id', user)
    if user_id is None:
        return False
    return str(user_id) in get_shadow_muted_user_ids(course_id)


def can_moderate_shadow_muted(user, course_id):
    """
    Return whether ``user`` should keep seeing shadow-muted content.

    True for forum Administrators / Moderators / Group Moderators /
    Community TAs, for the course team, and for global staff.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False

    course_key = _coerce_course_key(course_id)
    if course_key is None:
        return False

    cache = RequestCache(REQUEST_CACHE_NAMESPACE)
    cache_key = f'moderator:{user.id}:{course_key}'
    cached = cache.get_cached_response(cache_key)
    if cached.is_found:
        return cached.value

    # Imported lazily: comment_client imports this module, and pulling the
    # role/model layer in at import time risks an app-loading cycle.
    from common.djangoapps.student.roles import CourseInstructorRole, CourseStaffRole, GlobalStaff
    from openedx.core.djangoapps.django_comment_common.models import Role

    result = bool(
        GlobalStaff().has_user(user)
        or CourseStaffRole(course_key).has_user(user)
        or CourseInstructorRole(course_key).has_user(user)
        or Role.user_has_role_for_course(user, course_key, MODERATION_ROLES)
    )
    cache.set(cache_key, result)
    return result


def get_hidden_author_ids(course_id, viewer=None):
    """
    Return the author ids whose forum content must be hidden from the viewer.

    Empty when there is nothing to hide, when the viewer may moderate, or when
    there is no request-bound viewer at all (Celery tasks, management commands,
    and the notification-email handlers, which must keep seeing everything).
    """
    muted_ids = get_shadow_muted_user_ids(course_id)
    if not muted_ids:
        return frozenset()

    if viewer is None:
        viewer = get_current_user()

    # No request user => not a learner-facing read; do not filter.
    if viewer is None:
        return frozenset()

    if can_moderate_shadow_muted(viewer, course_id):
        return frozenset()

    viewer_id = getattr(viewer, 'id', None)
    if viewer_id is None:
        # Anonymous visitor: OST2 lets logged-out users read inline
        # discussions, and they are nobody's author, so hide everything muted.
        return muted_ids

    # The author always sees their own posts.
    return frozenset(muted_ids - {str(viewer_id)})


def _author_id(content):
    """Return the stringified author id of a forum content dict, or None."""
    user_id = content.get('user_id') if isinstance(content, dict) else None
    return None if user_id is None else str(user_id)


def _filter_children(content, hidden_ids):
    """
    Recursively drop hidden authors from a comment's ``children``.

    Returns the number of descendants removed. A hidden reply takes its own
    replies with it, exactly as a deleted reply would.
    """
    removed = 0
    for key in ('children', 'endorsed_responses', 'non_endorsed_responses'):
        replies = content.get(key)
        if not replies:
            continue
        kept = []
        for reply in replies:
            if _author_id(reply) in hidden_ids:
                removed += 1 + _count_descendants(reply)
                continue
            removed += _filter_children(reply, hidden_ids)
            kept.append(reply)
        content[key] = kept
    return removed


def _count_descendants(content):
    """Return how many replies hang off this content, at any depth."""
    total = 0
    for key in ('children', 'endorsed_responses', 'non_endorsed_responses'):
        for reply in content.get(key) or []:
            total += 1 + _count_descendants(reply)
    return total


def _decrement(content, key, amount):
    """Reduce a count field by ``amount``, never below zero."""
    if amount and isinstance(content.get(key), int):
        content[key] = max(0, content[key] - amount)


def filter_content_collection(response, course_id, viewer=None):
    """
    Filter a paginated thread or comment listing in place and return it.

    ``response`` is the raw forum-backend payload: a ``collection`` of content
    dicts plus ``page`` / ``num_pages`` and a ``thread_count`` or
    ``comment_count``.

    The count is reduced by what this page dropped. It cannot be made exact
    without asking the backend how many muted items exist across every page,
    and being slightly high is the harmless direction: the listing shows fewer
    posts than the count claims, which reads as ordinary pagination rather
    than as evidence that something is being hidden.
    """
    if not isinstance(response, dict):
        return response

    hidden_ids = get_hidden_author_ids(course_id, viewer)
    if not hidden_ids:
        return response

    collection = response.get('collection') or []
    kept = [thread for thread in collection if _author_id(thread) not in hidden_ids]
    removed = len(collection) - len(kept)
    if not removed:
        return response

    response['collection'] = kept
    _decrement(response, 'thread_count', removed)
    _decrement(response, 'comment_count', removed)
    return response


def filter_thread(thread, course_id, viewer=None):
    """
    Filter a single thread and its loaded responses in place.

    Returns None when the thread's own author is hidden from this viewer, which
    callers turn into the same "not found" the forum raises for a thread that
    does not exist -- so a direct link to a shadow-muted post 404s for peers
    instead of rendering.
    """
    if not isinstance(thread, dict):
        return thread

    hidden_ids = get_hidden_author_ids(course_id or thread.get('course_id'), viewer)
    if not hidden_ids:
        return thread

    if _author_id(thread) in hidden_ids:
        return None

    removed = _filter_children(thread, hidden_ids)
    if removed:
        _decrement(thread, 'comments_count', removed)
        _decrement(thread, 'resp_total', removed)
        _decrement(thread, 'non_endorsed_resp_total', removed)
    return thread


def filter_comment(comment, course_id, viewer=None):
    """
    Filter a single comment and its replies in place.

    Returns None when the comment's own author is hidden from this viewer.
    """
    if not isinstance(comment, dict):
        return comment

    hidden_ids = get_hidden_author_ids(course_id or comment.get('course_id'), viewer)
    if not hidden_ids:
        return comment

    if _author_id(comment) in hidden_ids:
        return None

    _filter_children(comment, hidden_ids)
    return comment


def filter_user_stats(stats, course_id, viewer=None):
    """
    Drop shadow-muted learners from the forum "Learners" activity stats.

    Keyed by username rather than user id, which is what the stats payload
    carries.
    """
    if not isinstance(stats, dict):
        return stats

    hidden_ids = get_hidden_author_ids(course_id, viewer)
    if not hidden_ids:
        return stats

    user_stats = stats.get('user_stats')
    if not user_stats:
        return stats

    from django.contrib.auth import get_user_model

    hidden_usernames = set(
        get_user_model().objects.filter(
            id__in=[int(user_id) for user_id in hidden_ids if str(user_id).isdigit()]
        ).values_list('username', flat=True)
    )
    if not hidden_usernames:
        return stats

    kept = [row for row in user_stats if row.get('username') not in hidden_usernames]
    removed = len(user_stats) - len(kept)
    if removed:
        stats['user_stats'] = kept
        _decrement(stats, 'count', removed)
    return stats


def set_shadow_mute(user, course_id, muted, actor=None, reason=''):
    """
    Apply or lift a shadow mute, returning the resulting boolean state.

    Lifting deactivates the row rather than deleting it, so the record of who
    muted whom, when and why survives.
    """
    from openedx.core.djangoapps.django_comment_common.models import ForumShadowMute

    course_key = _coerce_course_key(course_id)
    if course_key is None:
        raise ValueError(f'Invalid course id: {course_id!r}')

    defaults = {'is_active': muted, 'reason': reason}
    if actor is not None and actor.is_authenticated:
        defaults['created_by'] = actor

    record, created = ForumShadowMute.objects.get_or_create(
        user=user, course_id=course_key, defaults=defaults,
    )
    if not created and record.is_active != muted:
        record.is_active = muted
        if reason:
            record.reason = reason
        if actor is not None and actor.is_authenticated:
            record.created_by = actor
        record.save()

    RequestCache(REQUEST_CACHE_NAMESPACE).clear()
    log.info(
        'Shadow mute %s for user %s in %s by %s',
        'applied' if muted else 'lifted',
        user.username,
        course_key,
        getattr(actor, 'username', 'unknown'),
    )
    return muted
