"""
Discussions feature toggles
"""

from openedx.core.djangoapps.discussions.config.waffle import WAFFLE_FLAG_NAMESPACE
from openedx.core.djangoapps.waffle_utils import CourseWaffleFlag

# .. toggle_name: discussions.enable_discussions_mfe
# .. toggle_implementation: CourseWaffleFlag
# .. toggle_default: False
# .. toggle_description: Waffle flag to use the new MFE experience for discussions in the course tab
# .. toggle_use_cases: temporary, open_edx
# .. toggle_creation_date: 2021-11-05
# .. toggle_target_removal_date: 2022-12-05
ENABLE_DISCUSSIONS_MFE = CourseWaffleFlag(
    f"{WAFFLE_FLAG_NAMESPACE}.enable_discussions_mfe", __name__
)

# .. toggle_name: discussions.enable_new_thread_moderator_notifications
# .. toggle_implementation: CourseWaffleFlag
# .. toggle_default: False
# .. toggle_description: Waffle flag to toggle email notifications to course discussion moderators
#      (Administrators, Moderators, Community TAs, and matching Group Moderators) when a learner
#      creates a new discussion thread. Can be enabled/disabled per course.
# .. toggle_use_cases: open_edx
# .. toggle_creation_date: 2026-05-22
ENABLE_NEW_THREAD_MODERATOR_NOTIFICATIONS = CourseWaffleFlag(
    f'{WAFFLE_FLAG_NAMESPACE}.enable_new_thread_moderator_notifications', __name__
)
