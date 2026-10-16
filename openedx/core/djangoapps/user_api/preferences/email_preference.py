"""
The site-wide email preference: which mass email a user wants to get from the site.

It is an ordinary user preference, so users set it on the account settings page ("Site Preferences" ->
"Email Preferences") through the user preferences API. A user who never chose has no row and gets the default,
EMAIL_PREFERENCE_ALL.

* ``all``: site-wide email (see lms.djangoapps.bulk_email.site_email) and email from course instructors.
* ``course``: only email that instructors send to the learners enrolled in their course.
* ``none``: neither.

Account email, such as password resets and account activation, is always sent.
"""

from ..models import UserPreference

EMAIL_PREFERENCE_KEY = 'ost2_email_preference'

EMAIL_PREFERENCE_ALL = 'all'
EMAIL_PREFERENCE_COURSE = 'course'
EMAIL_PREFERENCE_NONE = 'none'
EMAIL_PREFERENCE_VALUES = (EMAIL_PREFERENCE_ALL, EMAIL_PREFERENCE_COURSE, EMAIL_PREFERENCE_NONE)


def get_email_preference(user):
    """
    Returns the user's email preference, one of EMAIL_PREFERENCE_VALUES.
    """
    value = UserPreference.objects.filter(user=user, key=EMAIL_PREFERENCE_KEY).values_list('value', flat=True).first()
    return value if value in EMAIL_PREFERENCE_VALUES else EMAIL_PREFERENCE_ALL


def set_email_preference(user, value):
    """
    Stores the user's email preference. `value` must be one of EMAIL_PREFERENCE_VALUES.
    """
    if value not in EMAIL_PREFERENCE_VALUES:
        raise ValueError(f"Unknown email preference {value!r}")
    UserPreference.objects.update_or_create(user=user, key=EMAIL_PREFERENCE_KEY, defaults={'value': value})


def get_user_ids_with_email_preference(*values):
    """
    Returns a queryset of the ids of the users who chose one of the given email preferences.

    Users who never chose are not in it, even when EMAIL_PREFERENCE_ALL is one of `values`.
    """
    return UserPreference.objects.filter(key=EMAIL_PREFERENCE_KEY, value__in=values).values_list('user_id', flat=True)
