"""
Views to support bulk email functionalities like opt-out.
"""


import logging

from django.contrib.auth.models import User  # lint-amnesty, pylint: disable=imported-auth-user
from django.core.signing import BadSignature
from django.http import Http404
from django.views.decorators.csrf import csrf_exempt
from eventtracking import tracker
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

from common.djangoapps.edxmako.shortcuts import render_to_response
from lms.djangoapps.bulk_email.api import get_unsubscribe_token_data
from lms.djangoapps.bulk_email.models import Optout
from lms.djangoapps.discussion.notification_prefs.views import UsernameCipher, UsernameDecryptionException
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
