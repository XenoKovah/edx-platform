"""
Utility methods for the account settings.
"""

import logging
import random
import re
import string

import waffle  # lint-amnesty, pylint: disable=invalid-django-waffle-import
from completion.models import BlockCompletion
from completion.waffle import ENABLE_COMPLETION_TRACKING_SWITCH
from django.conf import settings
from django.utils.translation import gettext as _
from edx_django_utils.user import generate_password
from social_django.models import UserSocialAuth

from common.djangoapps.student.models import AccountRecovery, Registration, get_retired_email_by_email
from openedx.core.djangoapps.site_configuration.models import SiteConfiguration
from openedx.core.djangoapps.theming.helpers import get_config_value_from_site_or_settings, get_current_site
from openedx.core.djangolib.oauth2_retirement_utils import retire_dot_oauth2_models
from xmodule.modulestore.django import modulestore  # lint-amnesty, pylint: disable=wrong-import-order

from ..models import UserRetirementStatus

ENABLE_SECONDARY_EMAIL_FEATURE_SWITCH = 'enable_secondary_email_feature'
LOGGER = logging.getLogger(__name__)

# Schemes permitted for any stored social link. Anything else (javascript:,
# data:, vbscript:, file:, ...) is rejected so that a profile link can never be
# used to run script in, or silently redirect, a viewer who clicks it -- which
# matters most for free-form platforms whose value is stored as the user typed
# it rather than rebuilt from a known host.
ALLOWED_SOCIAL_LINK_SCHEMES = ('http', 'https')


def _social_platform_config(platform_name):
    """Return the SOCIAL_PLATFORMS entry for a platform (or an empty dict)."""
    return settings.SOCIAL_PLATFORMS.get(platform_name, {})


def _is_freeform_platform(platform_name):
    """Whether the platform stores a user-supplied URL verbatim (e.g. a blog)."""
    return _social_platform_config(platform_name).get('url_type') == 'free'


def _strip_url_control_chars(value):
    """
    Remove the whitespace and ASCII control characters (NUL, tab, newline, etc.)
    that browsers silently drop when resolving a URL. Leaving them in would let
    a crafted value smuggle a dangerous scheme past a naive check -- for example
    a tab or newline inserted into "javascript:".
    """
    return re.sub(r'[\x00-\x20\x7f]+', '', value or '')


def _sanitize_freeform_url(new_social_link):
    """
    Return a safe absolute http(s) URL for a free-form social link, or None if
    the input is not a well-formed http/https URL.

    This is the security boundary for free-form platforms: unlike "stub"
    platforms (whose stored value is always rebuilt from a known host), the
    value here is what the user supplied, so it must be strictly validated.
    """
    candidate = _strip_url_control_chars(new_social_link)
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ALLOWED_SOCIAL_LINK_SCHEMES:
        return None
    if not parsed.netloc:
        return None
    # Re-serialize from the parsed components rather than echoing raw input.
    return parsed.geturl()


def validate_social_link(platform_name, new_social_link):
    """
    Given a new social link for a user, ensure that the link takes one of the
    following forms:

    1) A valid url that comes from the correct social site.
    2) A valid username.
    3) A blank value.

    For free-form platforms, ensure the link is a valid absolute http(s) URL.
    """
    formatted_social_link = format_social_link(platform_name, new_social_link)

    # Ensure that the new link is valid.
    if formatted_social_link is None:
        if _is_freeform_platform(platform_name):
            example = _social_platform_config(platform_name).get('example', 'https://example.com/')
            raise ValueError(_(
                'Make sure that you are providing a valid http(s) URL (for example, "{example}"). '
                'To remove the link, leave this field blank.'
            ).format(example=example))
        required_url_stub = _social_platform_config(platform_name).get('url_stub', '')
        raise ValueError(_(
            'Make sure that you are providing a valid username or a URL that contains "{url_stub}". '
            'To remove the link from your {platform_name} profile, leave this field blank.'
        ).format(url_stub=required_url_stub, platform_name=settings.PLATFORM_NAME))


def format_social_link(platform_name, new_social_link):
    """
    Given a user's social link, returns a safe absolute url for the social link.

    Returns the following based on the provided new_social_link:
    1) Given an empty string, returns ''
    2) For a "stub" platform, a valid username or URL returns
       'https://[host_prefix][url_stub][username]'
    3) For a "free-form" platform, a valid absolute http(s) URL is returned as-is.
    4) Given anything unparseable, returns None
    """
    # Blank social links should return '' or None as was passed in.
    if not new_social_link:
        return new_social_link

    # Free-form platforms (e.g. a personal blog) have no single canonical host to
    # rebuild from, so validate and store the URL the user provided.
    if _is_freeform_platform(platform_name):
        return _sanitize_freeform_url(new_social_link)

    username = _get_username_from_social_link(platform_name, new_social_link)
    if not username:
        return None

    # For security purposes, always build up the url rather than using input from user.
    config = _social_platform_config(platform_name)
    host_prefix = config.get('host_prefix', 'www.')
    return f'https://{host_prefix}{config["url_stub"]}{username}'


def _get_username_from_social_link(platform_name, new_social_link):
    """
    Returns the username given a social link.

    Uses the following logic to parse new_social_link into a username:
    1) If an empty string, returns it as the username.
    2) Given a URL, attempts to parse the username from the url and return it.
    3) Given a non-URL, returns the entire string as username if valid.
    4) If no valid username is found, returns None.
    """
    # Blank social links should return '' or None as was passed in.
    if not new_social_link:
        return new_social_link

    # Parse the social link as if it were a URL.
    parse_result = urlparse(new_social_link)
    url_domain_and_path = parse_result[1] + parse_result[2]

    config = _social_platform_config(platform_name)
    accepted_stubs = [config.get('url_stub', '')] + list(config.get('alt_url_stubs', []))

    # Default to treating the whole value as the username (covers a bare handle);
    # if it matches one of the accepted hosts, extract just the username part.
    username = new_social_link
    for stub in accepted_stubs:
        if not stub:
            continue
        url_stub = re.escape(stub)
        username_match = re.search(
            r'(www\.)?' + url_stub + r'(?P<username>.*?)[/]?$',
            url_domain_and_path,
            re.IGNORECASE,
        )
        if username_match:
            username = username_match.group('username')
            break

    # Ensure the username is a valid username.
    if not _is_valid_social_username(username):
        return None

    return username


def _is_valid_social_username(value):
    """
    Given a particular string, returns whether the string can be considered a safe username.
    This is a very liberal validation step, simply assuring forward slashes do not exist
    in the username.
    """
    return '/' not in value


def retrieve_last_sitewide_block_completed(user):
    """
    Completion utility
    From a given User object retrieve
    the last course block marked as 'completed' and construct a URL

    :param user: obj(User)
    :return: block_lms_url

    """
    if not ENABLE_COMPLETION_TRACKING_SWITCH.is_enabled():
        return

    latest_completions_by_course = BlockCompletion.latest_blocks_completed_all_courses(user)

    known_site_configs = [
        other_site_config.get_value('course_org_filter') for other_site_config in SiteConfiguration.objects.all()
        if other_site_config.get_value('course_org_filter')
    ]

    current_site_configuration = get_config_value_from_site_or_settings(
        name='course_org_filter',
        site=get_current_site()
    )

    # courses.edx.org has no 'course_org_filter'
    # however the courses within DO, but those entries are not found in
    # known_site_configs, which are White Label sites
    # This is necessary because the WL sites and courses.edx.org
    # have the same AWS RDS mySQL instance
    candidate_course = None
    candidate_block_key = None
    latest_date = None
    # Go through dict, find latest
    for course, [modified_date, block_key] in latest_completions_by_course.items():
        if not current_site_configuration:
            # This is a edx.org
            if course.org in known_site_configs:
                continue
            if not latest_date or modified_date > latest_date:
                candidate_course = course
                candidate_block_key = block_key
                latest_date = modified_date

        else:
            # This is a White Label site, and we should find candidates from the same site
            if course.org not in current_site_configuration:
                # Not the same White Label, or a edx.org course
                continue
            if not latest_date or modified_date > latest_date:
                candidate_course = course
                candidate_block_key = block_key
                latest_date = modified_date

    if not candidate_course:
        return

    lms_root = SiteConfiguration.get_value_for_org(candidate_course.org, "LMS_ROOT_URL", settings.LMS_ROOT_URL)

    try:
        item = modulestore().get_item(candidate_block_key, depth=1)
    except Exception as err:  # pylint: disable=broad-except
        LOGGER.exception(
            '[PROD-2877] Error retrieving resume block for user %s with raw error %r',
            user.username, err,
        )
        item = None

    if not (lms_root and item):
        return

    return "{lms_root}/courses/{course_key}/jump_to/{location}".format(
        lms_root=lms_root,
        course_key=str(item.location.course_key),
        location=str(item.location),
    )


def is_secondary_email_feature_enabled():
    """
    Checks to see if the django-waffle switch for enabling the secondary email feature is active

    Returns:
        Boolean value representing switch status
    """
    return waffle.switch_is_active(ENABLE_SECONDARY_EMAIL_FEATURE_SWITCH)


def create_retirement_request_and_deactivate_account(user):
    """
    Adds user to retirement queue, unlinks social auth accounts, changes user passwords
    and delete tokens and activation keys
    """
    # Add user to retirement queue.
    UserRetirementStatus.create_retirement(user)

    # Unlink LMS social auth accounts
    UserSocialAuth.objects.filter(user_id=user.id).delete()

    # Change LMS password & email
    user.email = get_retired_email_by_email(user.email)
    user.set_unusable_password()
    user.save()

    # TODO: Unlink social accounts & change password on each IDA.
    # Remove the activation keys sent by email to the user for account activation.
    Registration.objects.filter(user=user).delete()

    # Delete OAuth tokens associated with the user.
    retire_dot_oauth2_models(user)
    AccountRecovery.retire_recovery_email(user.id)


def username_suffix_generator(suffix_length=4):
    """
    Generates a random, alternating number and letter string for the purpose of
    appending to non-unique usernames. Alternating is less likey to produce
    a significant/meaningful substring like an offensive word.
    Whether the suffix starts with a letter or number is also randomized.
    """
    # pick from letters, or numbers
    choice_collections = [string.ascii_lowercase, string.digits]
    # randomize which collection to pick from first
    random.shuffle(choice_collections)
    output = ''
    for i in range(suffix_length):
        if (i % 2) == 0:
            output += random.choice(choice_collections[0])
        else:
            output += random.choice(choice_collections[1])
    return output


def handle_retirement_cancellation(retirement, email_address=None):
    """
    Do the following in order to cancel retirement for a given user:

    1. Load the user record using the retired email address -and- change the email address back.
    2. Reset users password so they can request a password reset and log in again.
    3. No need to delete the accompanying "permanent" retirement request record - it gets done via Django signal.
    """
    retirement.user.email = email_address if email_address else retirement.original_email

    retirement.user.set_password(generate_password(length=25))
    retirement.user.save()

    retirement.delete()
