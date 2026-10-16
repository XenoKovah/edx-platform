"""
Views to support bulk email functionalities like opt-out.
"""


import logging

from django.contrib.auth.models import User  # lint-amnesty, pylint: disable=imported-auth-user
from django.core.signing import BadSignature
from django.http import Http404
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from eventtracking import tracker
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

from common.djangoapps.edxmako.shortcuts import render_to_response
from lms.djangoapps.bulk_email.api import get_unsubscribe_token_data
from lms.djangoapps.bulk_email.models import Optout
from lms.djangoapps.bulk_email.site_email import get_email_preferences_link, get_site_email_unsubscribe_user_id
from lms.djangoapps.discussion.notification_prefs.views import UsernameCipher, UsernameDecryptionException
from openedx.core.djangoapps.user_api.preferences.email_preference import (
    EMAIL_PREFERENCE_ALL,
    EMAIL_PREFERENCE_COURSE,
    EMAIL_PREFERENCE_NONE,
    get_email_preference,
    set_email_preference
)
from openedx.core.lib.courses import get_course_by_id

log = logging.getLogger(__name__)


def opt_out_email_updates(request, token, course_id):
    """
    A view that let users opt out of any email updates.

    This meant is meant to be the target of an opt-out link or button.
    The `token` parameter must decrypt to a valid username.
    The `course_id` is the string course key of any course.

    Bulk email now links to `unsubscribe_from_course_email` instead; this view stays so that the links in
    email sent before that keep working.

    Raises a 404 if there are any errors parsing the input.
    """
    try:
        username = UsernameCipher().decrypt(token).decode("utf-8")
        user = User.objects.get(username=username)
        course_key = CourseKey.from_string(course_id)
        course = get_course_by_id(course_key, depth=0)
    except UnicodeDecodeError:
        raise Http404("base64url")  # lint-amnesty, pylint: disable=raise-missing-from
    except UsernameDecryptionException as exn:
        raise Http404(str(exn))  # lint-amnesty, pylint: disable=raise-missing-from
    except User.DoesNotExist:
        raise Http404("username")  # lint-amnesty, pylint: disable=raise-missing-from
    except InvalidKeyError:
        raise Http404("course")  # lint-amnesty, pylint: disable=raise-missing-from

    return _opt_out_response(request, user, course, course_key, course_id)


@csrf_exempt
def unsubscribe_from_course_email(request, token):
    """
    A view that lets a user opt out of a course's email, from the signed link that bulk email sends.

    The link is both in the message body and in its `List-Unsubscribe` header. A GET asks for confirmation,
    so mail scanners that follow links change nothing. A POST of `List-Unsubscribe=One-Click` (RFC 8058) opts
    out straight away: mail providers send it without cookies, so this view is CSRF exempt, and the signed
    token is what authorizes the request.

    Raises a 404 if the token is not valid.
    """
    try:
        user_id, course_id = get_unsubscribe_token_data(token)
        user = User.objects.get(id=user_id)
        course_key = CourseKey.from_string(course_id)
        course = get_course_by_id(course_key, depth=0)
    except BadSignature:
        raise Http404("token")  # lint-amnesty, pylint: disable=raise-missing-from
    except User.DoesNotExist:
        raise Http404("user")  # lint-amnesty, pylint: disable=raise-missing-from
    except InvalidKeyError:
        raise Http404("course")  # lint-amnesty, pylint: disable=raise-missing-from

    return _opt_out_response(request, user, course, course_key, course_id)


def _opt_out_response(request, user, course, course_key, course_id):
    """
    Opts the user out of the course's email if the request confirms it, and renders the matching page.

    A GET only asks for confirmation. A POST confirms with either the confirmation page's `unsubscribe`
    checkbox or the RFC 8058 one-click body `List-Unsubscribe=One-Click`.
    """
    one_click = request.POST.get('List-Unsubscribe') == 'One-Click'
    unsub_check = request.POST.get('unsubscribe', False) or one_click
    context = {
        'course': course,
        'unsubscribe': unsub_check
    }

    if request.method == 'GET':
        return render_to_response('bulk_email/confirm_unsubscribe.html', context)

    if request.method == 'POST' and unsub_check:
        Optout.objects.get_or_create(user=user, course_id=course_key)
        log.info(
            "User %s (%s) opted out of receiving emails from course %s%s",
            user.username,
            user.email,
            course_id,
            " (one-click)" if one_click else "",
        )

    event_name = 'edx.bulk_email.opt_out'
    event_data = {
        "username": user.username,
        "user_email": user.email,
        "user_id": user.id,
        "course_id": course_id,
    }
    with tracker.get_tracker().context(event_name, event_data):
        tracker.emit(
            event_name,
            event_data
        )

    return render_to_response('bulk_email/unsubscribe_success.html', context)


def _email_preference_labels():
    """
    Returns the names of the email preferences, as the account settings page shows them.
    """
    return {
        EMAIL_PREFERENCE_ALL: _("Receive all emails"),
        EMAIL_PREFERENCE_COURSE: _("Receive only emails sent to students enrolled in a course"),
        EMAIL_PREFERENCE_NONE: _("Receive no emails"),
    }


@csrf_exempt
def unsubscribe_from_site_email(request, token):
    """
    A view that stops site-wide email to a user, from the signed link in the footer of every site-wide email.

    It switches the user's email preference from "Receive all emails" to "Receive only emails sent to students
    enrolled in a course". A user who already chose that, or "Receive no emails", keeps their choice.

    Like `unsubscribe_from_course_email`, a GET asks for confirmation, so mail scanners that follow links change
    nothing, and a POST of `List-Unsubscribe=One-Click` (RFC 8058, from the `List-Unsubscribe` header) changes
    the preference straight away. The signed token is what authorizes the request, hence `csrf_exempt`.

    Raises a 404 if the token is not valid.
    """
    try:
        user = User.objects.get(id=get_site_email_unsubscribe_user_id(token))
    except BadSignature:
        raise Http404("token")  # lint-amnesty, pylint: disable=raise-missing-from
    except User.DoesNotExist:
        raise Http404("user")  # lint-amnesty, pylint: disable=raise-missing-from

    one_click = request.POST.get('List-Unsubscribe') == 'One-Click'
    confirmed = request.method == 'POST' and (one_click or bool(request.POST.get('unsubscribe')))
    preference = get_email_preference(user)
    context = {
        'preference_label': _email_preference_labels()[preference],
        'course_preference_label': _email_preference_labels()[EMAIL_PREFERENCE_COURSE],
        'preferences_link': get_email_preferences_link(),
        'receives_site_email': preference == EMAIL_PREFERENCE_ALL,
    }
    if not confirmed:
        return render_to_response('bulk_email/confirm_site_email_unsubscribe.html', context)

    if preference == EMAIL_PREFERENCE_ALL:
        set_email_preference(user, EMAIL_PREFERENCE_COURSE)
        log.info(
            "User %s (%s) opted out of site-wide email%s",
            user.username,
            user.email,
            " (one-click)" if one_click else "",
        )
        event_name = 'edx.bulk_email.site_email.opt_out'
        event_data = {
            "username": user.username,
            "user_email": user.email,
            "user_id": user.id,
            "email_preference": EMAIL_PREFERENCE_COURSE,
        }
        with tracker.get_tracker().context(event_name, event_data):
            tracker.emit(event_name, event_data)
    return render_to_response('bulk_email/site_email_unsubscribe_success.html', context)
