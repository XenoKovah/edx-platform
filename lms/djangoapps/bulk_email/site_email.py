"""
Site-wide email: email sent to the users of the whole site rather than to the learners of one course.

Only users whose site-wide email preference is "Receive all emails" get it (see
openedx.core.djangoapps.user_api.preferences.email_preference). Every message ends with a footer whose signed
link switches the recipient to "Receive only emails sent to students enrolled in a course", plus a link to the
account settings page where they can change their preference. The same signed link goes in the message's
`List-Unsubscribe` headers (RFC 2369 and RFC 8058), which Gmail and Yahoo require on bulk mail.

Send it with the `send_site_email` management command.
"""

import html

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse

from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.user_api.models import UserRetirementRequest
from openedx.core.djangoapps.user_api.preferences.email_preference import (
    EMAIL_PREFERENCE_COURSE,
    EMAIL_PREFERENCE_NONE,
    get_user_ids_with_email_preference
)

User = get_user_model()

SITE_EMAIL_UNSUBSCRIBE_TOKEN_SALT = 'lms.djangoapps.bulk_email.site_email_unsubscribe'

# The anchor of the Email Preferences field on the account settings page.
EMAIL_PREFERENCES_ANCHOR = '#email-preferences'

FOOTER_TEXT = (
    "To not receive emails like this in the future, click here: {unsubscribe_link}\n"
    "or go to your account preferences: {preferences_link}\n"
)
FOOTER_HTML = (
    '<p style="font-size: 12px; color: #6c757d;">'
    'To not receive emails like this in the future, <a href="{unsubscribe_link}">click here</a>, '
    'or go to your <a href="{preferences_link}">account preferences</a>.'
    '</p>'
)

# Placeholders that a message body may use, filled in for each recipient.
PLACEHOLDERS = ('{name}', '{username}', '{email}')


def get_site_email_recipients():
    """
    Returns the users who should get site-wide email, ordered by id.

    That is the active accounts that have not opted out of site-wide email, leaving out the accounts that are
    disabled (unusable password, the same test bulk email uses) or being retired.
    """
    return (
        User.objects.filter(is_active=True)
        .exclude(email='')
        .exclude(password__startswith='!')
        .exclude(id__in=get_user_ids_with_email_preference(EMAIL_PREFERENCE_COURSE, EMAIL_PREFERENCE_NONE))
        .exclude(id__in=UserRetirementRequest.objects.values('user_id'))
        .order_by('id')
    )


def get_site_email_unsubscribe_link(user_id):
    """
    Returns the link that stops site-wide email to a user without them having to log in.

    The token in the link is signed with SECRET_KEY, so it can't be forged or edited to change someone else's
    preference.
    """
    lms_root_url = configuration_helpers.get_value('LMS_ROOT_URL', settings.LMS_ROOT_URL)
    token = signing.Signer(salt=SITE_EMAIL_UNSUBSCRIBE_TOKEN_SALT).sign_object(user_id)
    return lms_root_url + reverse('bulk_email_site_email_unsubscribe', kwargs={'token': token})


def get_site_email_unsubscribe_user_id(token):
    """
    Returns the id of the user that a token from `get_site_email_unsubscribe_link` was made for.

    Raises `django.core.signing.BadSignature` if the token was not made by `get_site_email_unsubscribe_link`.
    """
    return signing.Signer(salt=SITE_EMAIL_UNSUBSCRIBE_TOKEN_SALT).unsign_object(token)


def get_email_preferences_link():
    """
    Returns the link to the Email Preferences field on the account settings page.
    """
    account_url = configuration_helpers.get_value('ACCOUNT_MICROFRONTEND_URL', settings.ACCOUNT_MICROFRONTEND_URL)
    if not account_url:
        lms_root_url = configuration_helpers.get_value('LMS_ROOT_URL', settings.LMS_ROOT_URL)
        account_url = f'{lms_root_url}/account/settings'
    return account_url.rstrip('/') + '/' + EMAIL_PREFERENCES_ANCHOR


def get_site_email_from_address():
    """
    Returns the default From address of site-wide email: the one bulk email uses.
    """
    return settings.BULK_EMAIL_DEFAULT_FROM_EMAIL or settings.DEFAULT_FROM_EMAIL


def _fill_placeholders(body, user, escape):
    """
    Replaces the PLACEHOLDERS in a message body with the recipient's details.

    Only those exact strings are replaced, so a body may contain other braces (CSS, code samples) as is.
    """
    name = getattr(getattr(user, 'profile', None), 'name', '') or user.username
    values = {'{name}': name, '{username}': user.username, '{email}': user.email}
    for placeholder in PLACEHOLDERS:
        value = values[placeholder]
        body = body.replace(placeholder, html.escape(value) if escape else value)
    return body


def build_site_email(user, subject, text_body, html_body=None, from_email=None):
    """
    Returns the site-wide email to send to `user`, as an EmailMultiAlternatives.

    `text_body` (and `html_body`, if given) get the footer appended and their PLACEHOLDERS filled in. The
    message gets the `List-Unsubscribe` headers.
    """
    links = {
        'unsubscribe_link': get_site_email_unsubscribe_link(user.id),
        'preferences_link': get_email_preferences_link(),
    }
    text = _fill_placeholders(text_body, user, escape=False).rstrip('\n')
    text = f'{text}\n\n----\n{FOOTER_TEXT.format(**links)}'
    message = EmailMultiAlternatives(
        subject,
        text,
        from_email or get_site_email_from_address(),
        [user.email],
        headers={
            'List-Unsubscribe': f'<{links["unsubscribe_link"]}>',
            'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
        },
    )
    if html_body:
        footer = FOOTER_HTML.format(**{key: html.escape(value) for key, value in links.items()})
        body = _fill_placeholders(html_body, user, escape=True)
        index = body.lower().rfind('</body>')
        body = body[:index] + footer + body[index:] if index != -1 else body + footer
        message.attach_alternative(body, 'text/html')
    return message
