"""
What to tell a signed-in user whose account is not activated yet.

Registration signs the new account in, but once that session ends the user cannot sign in again until the
account is activated (see views/login.py). So every page tells them, until they activate, where the activation
email went and how to fix a wrong address. This module decides what the notice says; two surfaces show it:

* the learner MFEs (tutor-indigo's account-activation banner), through the ``account_activation_notice`` API;
* the LMS's own pages (lms/templates/page_banner.html).

ost2-tutor-plugins' host-scripts/ost2_bounce_delete.py records a bounced activation email in the
``ost2_activation_bounce`` user preference, a JSON object
``{"address": ..., "bounced_at": <ISO 8601>, "delete_after": <ISO 8601> or null}``. It deletes the account
after ``delete_after`` unless the email address was changed, and it removes the preference once the address no
longer matches. Confirming an email change activates the account (student.views.confirm_email_change).
"""

import json
import logging
from datetime import timezone

from django.utils.dateparse import parse_datetime

from common.djangoapps.student.models import PendingEmailChange
from openedx.core.djangoapps.user_api.models import UserPreference

log = logging.getLogger(__name__)

ACTIVATION_BOUNCE_PREF_KEY = 'ost2_activation_bounce'

NOTICE_UNACTIVATED = 'unactivated'
NOTICE_BOUNCED = 'bounced'
NOTICE_EMAIL_CHANGE_PENDING = 'email_change_pending'


def get_account_activation_notice(user):
    """
    Returns None when `user` needs no notice (anonymous or already active), else a dict:

    * ``variant``: NOTICE_EMAIL_CHANGE_PENDING when the user asked to change their email address and has not
      confirmed the new one yet, else NOTICE_BOUNCED when the activation email to their current address
      bounced, else NOTICE_UNACTIVATED.
    * ``email``: the current email address.
    * ``pending_email``: the new address waiting for confirmation, or None.
    * ``bounced``: whether the activation email to ``email`` bounced.
    * ``delete_after``: when the bounced account may be deleted (an aware datetime), or None when it bounced
      but is not due for deletion.
    """
    if not user.is_authenticated or user.is_active:
        return None

    bounce = _get_activation_bounce(user)
    pending_email = PendingEmailChange.objects.filter(user=user).values_list('new_email', flat=True).first()
    if pending_email:
        variant = NOTICE_EMAIL_CHANGE_PENDING
    elif bounce is not None:
        variant = NOTICE_BOUNCED
    else:
        variant = NOTICE_UNACTIVATED
    return {
        'variant': variant,
        'email': user.email,
        'pending_email': pending_email,
        'bounced': bounce is not None,
        'delete_after': bounce['delete_after'] if bounce else None,
    }


def clear_activation_bounce(user):
    """
    Forgets that the activation email to the user's address bounced; called when the address changes.
    """
    UserPreference.objects.filter(user=user, key=ACTIVATION_BOUNCE_PREF_KEY).delete()


def _get_activation_bounce(user):
    """
    Returns {'delete_after': aware datetime or None} when the activation email to the user's current address
    bounced, else None. A preference for another address (one the user has since changed away from) or one that
    cannot be read counts as no bounce.
    """
    value = UserPreference.objects.filter(
        user=user, key=ACTIVATION_BOUNCE_PREF_KEY
    ).values_list('value', flat=True).first()
    if not value:
        return None
    try:
        bounce = json.loads(value)
        address = bounce['address']
        delete_after = bounce.get('delete_after')
        delete_after = parse_datetime(delete_after) if delete_after is not None else None
    except (ValueError, TypeError, KeyError):
        log.warning('Ignoring unreadable %s preference of user %s', ACTIVATION_BOUNCE_PREF_KEY, user.id)
        return None
    if not isinstance(address, str) or address.lower() != user.email.lower():
        return None
    if delete_after is not None and delete_after.tzinfo is None:
        delete_after = delete_after.replace(tzinfo=timezone.utc)
    return {'delete_after': delete_after}
