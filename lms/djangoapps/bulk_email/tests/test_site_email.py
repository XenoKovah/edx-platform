"""
Tests for site-wide email: recipients, the message, the unsubscribe link and the send_site_email command.
"""

import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core import mail, signing
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.test.client import Client
from django.test.utils import override_settings
from django.urls import reverse

from common.djangoapps.student.tests.factories import UserFactory
from lms.djangoapps.bulk_email.site_email import (
    SITE_EMAIL_UNSUBSCRIBE_TOKEN_SALT,
    build_site_email,
    get_email_preferences_link,
    get_site_email_recipients,
    get_site_email_unsubscribe_link
)
from openedx.core.djangoapps.user_api.models import UserRetirementRequest
from openedx.core.djangoapps.user_api.preferences.email_preference import (
    EMAIL_PREFERENCE_ALL,
    EMAIL_PREFERENCE_COURSE,
    EMAIL_PREFERENCE_NONE,
    get_email_preference,
    set_email_preference
)


@override_settings(
    SECRET_KEY="test secret key",
    LMS_ROOT_URL='https://lms.example.com',
    ACCOUNT_MICROFRONTEND_URL='https://apps.example.com/account/',
    BULK_EMAIL_DEFAULT_FROM_EMAIL='bulk@example.com',
)
class SiteEmailTest(TestCase):
    """
    Tests for who gets site-wide email and what it looks like.
    """

    def setUp(self):
        super().setUp()
        self.user = UserFactory.create(username='alice', email='alice@example.com')
        self.user.profile.name = 'Alice <Liddell>'
        self.user.profile.save()

    def test_recipients(self):
        """
        Only active, usable, non-retiring accounts that still want site-wide email are recipients.
        """
        explicit_all = UserFactory.create()
        set_email_preference(explicit_all, EMAIL_PREFERENCE_ALL)
        course_only = UserFactory.create()
        set_email_preference(course_only, EMAIL_PREFERENCE_COURSE)
        no_email = UserFactory.create()
        set_email_preference(no_email, EMAIL_PREFERENCE_NONE)
        UserFactory.create(is_active=False)
        disabled = UserFactory.create()
        disabled.set_unusable_password()
        disabled.save()
        retiring = UserFactory.create()
        UserRetirementRequest.create_retirement_request(retiring)

        assert list(get_site_email_recipients()) == [self.user, explicit_all]

    def test_message(self):
        """
        The message gets the footer, the placeholders filled in and the one-click unsubscribe headers.
        """
        message = build_site_email(
            self.user, 'Hello', 'Hi {name}, your username is {username} {not_a_placeholder}.\n',
        )
        unsubscribe_link = get_site_email_unsubscribe_link(self.user.id)
        preferences_link = get_email_preferences_link()

        assert preferences_link == 'https://apps.example.com/account/#email-preferences'
        assert unsubscribe_link.startswith('https://lms.example.com/bulk_email/email/site/unsubscribe/')
        assert message.to == ['alice@example.com']
        assert message.from_email == 'bulk@example.com'
        assert message.body == (
            'Hi Alice <Liddell>, your username is alice {not_a_placeholder}.\n\n'
            '----\n'
            f'To not receive emails like this in the future, click here: {unsubscribe_link}\n'
            f'or go to your account preferences: {preferences_link}\n'
        )
        assert message.alternatives == []
        headers = message.message()
        assert headers['List-Unsubscribe'] == f'<{unsubscribe_link}>'
        assert headers['List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'

    def test_html_message(self):
        """
        The HTML footer goes before </body> with the user's details HTML-escaped.
        """
        message = build_site_email(
            self.user, 'Hello', 'Hi', '<html><body><p>Hi {name}</p></body></html>', from_email='me@example.com',
        )
        html_body, mimetype = message.alternatives[0]
        assert mimetype == 'text/html'
        assert message.from_email == 'me@example.com'
        assert html_body.startswith('<html><body><p>Hi Alice &lt;Liddell&gt;</p><p ')
        assert html_body.endswith('</p></body></html>')
        assert f'<a href="{get_site_email_unsubscribe_link(self.user.id)}">click here</a>' in html_body
        assert f'<a href="{get_email_preferences_link()}">account preferences</a>' in html_body

    @override_settings(ACCOUNT_MICROFRONTEND_URL=None)
    def test_preferences_link_without_account_mfe(self):
        assert get_email_preferences_link() == 'https://lms.example.com/account/settings/#email-preferences'


@override_settings(SECRET_KEY="test secret key")
class UnsubscribeFromSiteEmailViewTest(TestCase):
    """
    Tests for the signed link that stops site-wide email, including RFC 8058 one-click unsubscribe.
    """

    def setUp(self):
        super().setUp()
        self.user = UserFactory.create()
        self.url = get_site_email_unsubscribe_link(self.user.id)
        # Mail providers POST the one-click unsubscribe with no cookies and no CSRF token.
        self.client = Client(enforce_csrf_checks=True)

    def _token_url(self, token):
        """Returns the unsubscribe URL for a token."""
        return reverse('bulk_email_site_email_unsubscribe', kwargs={'token': token})

    def test_get_asks_for_confirmation(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Stop receiving site-wide emails')
        self.assertContains(response, get_email_preferences_link())
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_ALL

    def test_confirm_form_post(self):
        response = self.client.post(self.url, {'unsubscribe': 'true'})
        self.assertContains(response, 'You will no longer receive site-wide emails')
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_COURSE

    def test_one_click_post(self):
        response = self.client.post(
            self.url, 'List-Unsubscribe=One-Click', content_type='application/x-www-form-urlencoded',
        )
        assert response.status_code == 200
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_COURSE

    def test_post_without_confirmation(self):
        response = self.client.post(self.url, {})
        self.assertContains(response, 'Stop receiving site-wide emails')
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_ALL

    def test_keeps_no_email_preference(self):
        """
        Someone who chose "Receive no emails" must not be switched back to course email.
        """
        set_email_preference(self.user, EMAIL_PREFERENCE_NONE)
        self.assertContains(self.client.get(self.url), 'You are not receiving site-wide emails')
        response = self.client.post(self.url, {'List-Unsubscribe': 'One-Click'})
        self.assertContains(response, 'Receive no emails')
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_NONE

    def test_bad_tokens(self):
        other_user = UserFactory.create()
        good_token = signing.Signer(salt=SITE_EMAIL_UNSUBSCRIBE_TOKEN_SALT).sign_object(self.user.id)
        for token in (
            good_token[:-1] + ('A' if good_token[-1] != 'A' else 'B'),  # tampered signature
            signing.Signer(salt='lms.djangoapps.bulk_email.unsubscribe').sign_object(other_user.id),  # other salt
            signing.Signer(salt=SITE_EMAIL_UNSUBSCRIBE_TOKEN_SALT).sign_object(other_user.id)[:-2] + good_token[-2:],
            signing.Signer(salt=SITE_EMAIL_UNSUBSCRIBE_TOKEN_SALT).sign_object(10 ** 9),  # no such user
        ):
            response = self.client.post(self._token_url(token), {'List-Unsubscribe': 'One-Click'})
            assert response.status_code == 404, token
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_ALL
        assert get_email_preference(other_user) == EMAIL_PREFERENCE_ALL


@override_settings(SECRET_KEY="test secret key", BULK_EMAIL_DEFAULT_FROM_EMAIL='bulk@example.com')
@patch('lms.djangoapps.bulk_email.management.commands.send_site_email.time.sleep', lambda seconds: None)
class SendSiteEmailCommandTest(TestCase):
    """
    Tests for the send_site_email management command.
    """

    def setUp(self):
        super().setUp()
        self.users = [UserFactory.create() for _ in range(4)]
        set_email_preference(self.users[3], EMAIL_PREFERENCE_COURSE)
        self.dir = tempfile.mkdtemp()
        self.body_path = os.path.join(self.dir, 'body.txt')
        with open(self.body_path, 'w', encoding='utf-8') as body_file:
            body_file.write('Hello {username}\n')

    def _call(self, *args):
        """Runs the command for campaign test-1 and returns its output."""
        out = StringIO()
        call_command(
            'send_site_email', '--campaign', 'test-1', '--subject', 'Hi', '--text-file', self.body_path,
            '--log-dir', self.dir, *args, stdout=out,
        )
        return out.getvalue()

    def test_dry_run_sends_nothing(self):
        out = self._call('--dry-run')
        assert 'DRY RUN: would send to 3 recipient(s)' in out
        assert f'Hello {self.users[0].username}' in out
        assert 'List-Unsubscribe-Post: List-Unsubscribe=One-Click' in out
        assert len(mail.outbox) == 0

    def test_send_resumes_where_it_stopped(self):
        out = self._call('--limit', '2')
        assert 'DONE sent=2 skipped=0 failed=0' in out
        assert [m.to[0] for m in mail.outbox] == [self.users[0].email, self.users[1].email]

        out = self._call()
        assert '1 recipient(s) left' in out
        assert [m.to[0] for m in mail.outbox][2:] == [self.users[2].email]

        out = self._call()
        assert '0 recipient(s) left' in out
        assert len(mail.outbox) == 3
        with open(os.path.join(self.dir, 'test-1.log'), encoding='utf-8') as log_file:
            assert [line.split()[:2] for line in log_file] == [['SENT', str(user.id)] for user in self.users[:3]]

    def test_skips_users_who_opt_out_during_the_run(self):
        with patch(
            'lms.djangoapps.bulk_email.management.commands.send_site_email.get_email_preference',
            side_effect=[EMAIL_PREFERENCE_ALL, EMAIL_PREFERENCE_NONE, EMAIL_PREFERENCE_ALL],
        ):
            out = self._call()
        assert 'DONE sent=2 skipped=1 failed=0' in out
        assert [m.to[0] for m in mail.outbox] == [self.users[0].email, self.users[2].email]

    def test_test_send(self):
        out = self._call('--to', self.users[3].username)
        assert 'preference course' in out
        assert [m.to[0] for m in mail.outbox] == [self.users[3].email]
        # A test send doesn't count as sent.
        assert '3 recipient(s) left' in self._call('--dry-run')

    def test_stops_after_repeated_failures(self):
        with patch('django.core.mail.EmailMessage.send', side_effect=OSError('down')):
            with self.assertRaisesRegex(CommandError, '3 failures in a row'):
                self._call()

    def test_rejects_bad_campaign_names(self):
        with self.assertRaises(CommandError):
            call_command(
                'send_site_email', '--campaign', '../x', '--subject', 'Hi', '--text-file', self.body_path,
                '--dry-run', stdout=StringIO(),
            )
