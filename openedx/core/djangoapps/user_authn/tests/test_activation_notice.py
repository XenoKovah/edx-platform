"""
Tests for the notice shown to signed-in users whose account is not activated yet.
"""

import json
from datetime import datetime, timezone

from crum import set_current_request
from django.contrib.auth.models import AnonymousUser, User  # lint-amnesty, pylint: disable=imported-auth-user
from django.test import RequestFactory, TestCase

from common.djangoapps.edxmako.shortcuts import render_to_string
from common.djangoapps.student.tests.factories import PendingEmailChangeFactory, UserFactory
from openedx.core.djangoapps.user_api.models import UserPreference
from openedx.core.djangolib.testing.utils import skip_unless_lms

from ..activation_notice import (
    ACTIVATION_BOUNCE_PREF_KEY,
    NOTICE_BOUNCED,
    NOTICE_EMAIL_CHANGE_PENDING,
    NOTICE_UNACTIVATED,
    clear_activation_bounce,
    get_account_activation_notice,
)


class AccountActivationNoticeTest(TestCase):
    """
    Tests for get_account_activation_notice and clear_activation_bounce.
    """

    def setUp(self):
        super().setUp()
        self.user = UserFactory(email='learner@example.com', is_active=False)

    def set_bounce(self, address='learner@example.com', delete_after='2026-11-02T07:42:00+00:00'):
        UserPreference.objects.update_or_create(user=self.user, key=ACTIVATION_BOUNCE_PREF_KEY, defaults={
            'value': json.dumps({
                'address': address, 'bounced_at': '2026-10-26T07:43:00+00:00', 'delete_after': delete_after,
            }),
        })

    def test_no_notice_for_active_or_anonymous_users(self):
        assert get_account_activation_notice(UserFactory()) is None
        assert get_account_activation_notice(AnonymousUser()) is None

    def test_unactivated(self):
        assert get_account_activation_notice(self.user) == {
            'variant': NOTICE_UNACTIVATED,
            'email': 'learner@example.com',
            'pending_email': None,
            'bounced': False,
            'delete_after': None,
        }

    def test_reads_the_account_afresh(self):
        # e.g. on the page that confirms an email change, which activates the account through another instance
        User.objects.filter(pk=self.user.pk).update(email='learner@example.org')
        assert get_account_activation_notice(self.user)['email'] == 'learner@example.org'
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        assert get_account_activation_notice(self.user) is None

    def test_bounced(self):
        self.set_bounce(address='Learner@Example.com')
        assert get_account_activation_notice(self.user) == {
            'variant': NOTICE_BOUNCED,
            'email': 'learner@example.com',
            'pending_email': None,
            'bounced': True,
            'delete_after': datetime(2026, 11, 2, 7, 42, tzinfo=timezone.utc),
        }

    def test_bounced_without_deletion_date(self):
        self.set_bounce(delete_after=None)
        notice = get_account_activation_notice(self.user)
        assert notice['variant'] == NOTICE_BOUNCED
        assert notice['delete_after'] is None

    def test_bounce_of_another_address_is_ignored(self):
        self.set_bounce(address='lerner@example.com')
        notice = get_account_activation_notice(self.user)
        assert notice['variant'] == NOTICE_UNACTIVATED
        assert not notice['bounced']

    def test_unreadable_bounce_is_ignored(self):
        for value in ('not json', '[]', '{}', json.dumps({'address': None})):
            UserPreference.objects.update_or_create(
                user=self.user, key=ACTIVATION_BOUNCE_PREF_KEY, defaults={'value': value}
            )
            assert get_account_activation_notice(self.user)['variant'] == NOTICE_UNACTIVATED, value

    def test_unreadable_deletion_date_is_left_out(self):
        self.set_bounce(delete_after='soon')
        notice = get_account_activation_notice(self.user)
        assert notice['variant'] == NOTICE_BOUNCED
        assert notice['delete_after'] is None

    def test_pending_email_change(self):
        self.set_bounce()
        PendingEmailChangeFactory(user=self.user, new_email='learner@example.org')
        assert get_account_activation_notice(self.user) == {
            'variant': NOTICE_EMAIL_CHANGE_PENDING,
            'email': 'learner@example.com',
            'pending_email': 'learner@example.org',
            'bounced': True,
            'delete_after': datetime(2026, 11, 2, 7, 42, tzinfo=timezone.utc),
        }

    def test_clear_activation_bounce(self):
        self.set_bounce()
        clear_activation_bounce(self.user)
        assert not UserPreference.objects.filter(user=self.user, key=ACTIVATION_BOUNCE_PREF_KEY).exists()


@skip_unless_lms
class AccountActivationBannerTest(TestCase):
    """
    The LMS's own pages show the notice above the site header.
    """

    def render_banner(self, user, **context):
        request = RequestFactory().get('/courses')
        request.user = user
        return render_to_string(
            'account_activation_notice.html', {'request': request, 'csrf_token': 'token', **context}
        )

    def test_above_the_site_header(self):
        request = RequestFactory().get('/404')
        request.user = UserFactory(is_active=False)
        set_current_request(request)
        self.addCleanup(set_current_request, None)
        html = render_to_string('static_templates/404.html', {}, request=request)  # a whole page
        assert '<div class="ost2-activation-banner">' in html
        assert html.index('<div class="ost2-activation-banner">') < html.index('<header class="global-header')

    def test_no_banner_for_active_users(self):
        assert 'Activate your account' not in self.render_banner(UserFactory())

    def test_no_banner_on_pages_without_the_site_header(self):
        # e.g. a unit rendered into the learning MFE's iframe, under the MFE's own banner
        html = self.render_banner(UserFactory(is_active=False), disable_header=True)
        assert 'Activate your account' not in html
        assert 'ost2-activation-resend' not in html

    def test_unactivated(self):
        html = self.render_banner(UserFactory(email='learner@example.com', is_active=False))
        assert '<div class="ost2-activation-heading">Activate your account</div>' in html
        assert '<p>Your account isn&#39;t activated yet' in html
        assert '&lt;' not in html
        assert '<strong>learner@example.com</strong>' in html
        assert 'Resend activation email' in html
        assert '/api/send_account_activation_email' in html
        assert 'href="http://account-mfe/#email"' in html
        assert 'ost2-activation-alert-warning' in html

    def test_bounced(self):
        user = UserFactory(email='learner@example.com', is_active=False)
        UserPreference.objects.create(user=user, key=ACTIVATION_BOUNCE_PREF_KEY, value=json.dumps({
            'address': 'learner@example.com', 'bounced_at': '2026-10-26T07:43:00+00:00',
            'delete_after': '2026-11-02T07:42:00+00:00',
        }))
        html = self.render_banner(user)
        assert 'Your activation email bounced' in html
        assert '<strong>November 2, 2026</strong>' in html
        assert 'Change your email address' in html
        assert 'Resend activation email' not in html
        assert 'ost2-activation-alert-danger' in html

    def test_pending_email_change(self):
        user = UserFactory(email='learner@example.com', is_active=False)
        PendingEmailChangeFactory(user=user, new_email='learner@example.org')
        html = self.render_banner(user)
        assert 'Confirm your new email address' in html
        assert '<strong>learner@example.org</strong>' in html
        assert 'deleted' not in html
