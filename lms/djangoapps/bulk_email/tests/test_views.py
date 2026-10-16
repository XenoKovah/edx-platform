"""
Test the bulk email opt out view.
"""

import ddt
import pytest
from django.core import signing
from django.http import Http404
from django.test.client import Client, RequestFactory
from django.test.utils import override_settings
from django.urls import reverse

from common.djangoapps.student.tests.factories import UserFactory
from lms.djangoapps.bulk_email.api import UNSUBSCRIBE_TOKEN_SALT, get_unsubscribed_link
from lms.djangoapps.bulk_email.models import Optout
from lms.djangoapps.bulk_email.views import opt_out_email_updates
from lms.djangoapps.discussion.notification_prefs.views import UsernameCipher
from openedx.core.lib.tests import attr
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase  # lint-amnesty, pylint: disable=wrong-import-order
from xmodule.modulestore.tests.factories import CourseFactory  # lint-amnesty, pylint: disable=wrong-import-order


@attr(shard=1)
@ddt.ddt
@override_settings(SECRET_KEY="test secret key")
class OptOutEmailUpdatesViewTest(ModuleStoreTestCase):
    """
    Check the opt out email functionality.
    """
    def setUp(self):
        super().setUp()
        self.user = UserFactory.create(username="testuser1", email='test@example.com')
        self.course = CourseFactory.create(run='testcourse1', display_name='Test Course Title')
        self.token = UsernameCipher.encrypt('testuser1')
        self.request_factory = RequestFactory()
        self.url = reverse('bulk_email_opt_out', args=[self.token, str(self.course.id)])

        # Ensure we start with no opt-out records
        assert Optout.objects.count() == 0

    def test_opt_out_email_confirm(self):
        """
        Ensure that the default GET view asks for confirmation.
        """
        response = self.client.get(self.url)
        self.assertContains(response, "confirm unsubscribe from")
        assert Optout.objects.count() == 0

    def test_opt_out_email_unsubscribe(self):
        """
        Ensure that the POSTing "confirm" creates the opt-out record.
        """
        response = self.client.post(self.url, {'unsubscribe': True})
        self.assertContains(response, "You have successfully unsubscribed from")
        assert Optout.objects.count() == 1

    def test_opt_out_email_cancel(self):
        """
        Ensure that the POSTing "cancel" does not create the opt-out record
        """
        response = self.client.post(self.url)
        self.assertContains(response, "You have not been unsubscribed from")
        assert Optout.objects.count() == 0

    @ddt.data(
        ("ZOMG INVALID BASE64 CHARS!!!", "base64url", False),
        ("Non-ASCII\xff".encode(), "base64url", False),
        ("D6L8Q01ztywqnr3coMOlq0C3DG05686lXX_1ArEd0ok", "base64url", False),
        ("AAAAAAAAAAA=", "initialization_vector", False),
        ("nMXVK7PdSlKPOovci-M7iqS09Ux8VoCNDJixLBmj", "aes", False),
        ("AAAAAAAAAAAAAAAAAAAAAMoazRI7ePLjEWXN1N7keLw=", "padding", False),
        ("AAAAAAAAAAAAAAAAAAAAACpyUxTGIrUjnpuUsNi7mAY=", "username", False),
        ("_KHGdCAUIToc4iaRGy7K57mNZiiXxO61qfKT08ExlY8=", "course", 'course-v1:testcourse'),
    )
    @ddt.unpack
    def test_unsubscribe_invalid_token(self, token, message, course):
        """
        Make sure that view returns 404 in case token is not valid
        """
        request = self.request_factory.get("dummy")
        with pytest.raises(Http404) as err:
            opt_out_email_updates(request, token, course)
            assert message in err


@attr(shard=1)
@ddt.ddt
@override_settings(SECRET_KEY="test secret key")
class UnsubscribeFromCourseEmailViewTest(ModuleStoreTestCase):
    """
    Check the signed unsubscribe link that bulk email sends, including RFC 8058 one-click unsubscribe.
    """
    def setUp(self):
        super().setUp()
        self.user = UserFactory.create(username="testuser1", email='test@example.com')
        self.other_user = UserFactory.create(username="testuser2", email='test2@example.com')
        self.course = CourseFactory.create(run='testcourse1', display_name='Test Course Title')
        self.url = get_unsubscribed_link(self.user.id, str(self.course.id))
        # Mail providers POST the one-click unsubscribe with no cookies and no CSRF token.
        self.client = Client(enforce_csrf_checks=True)

        # Ensure we start with no opt-out records
        assert Optout.objects.count() == 0

    def _token_url(self, token):
        return reverse('bulk_email_unsubscribe', kwargs={'token': token})

    def _sign(self, payload, salt=UNSUBSCRIBE_TOKEN_SALT):
        return signing.Signer(salt=salt).sign_object(payload)

    def test_get_asks_for_confirmation(self):
        """
        Ensure that following the link (as mail scanners do) only asks for confirmation.
        """
        response = self.client.get(self.url)
        self.assertContains(response, "confirm unsubscribe from")
        assert Optout.objects.count() == 0

    def test_one_click_post_unsubscribes(self):
        """
        Ensure that the RFC 8058 one-click POST opts the user out of the course straight away.
        """
        response = self.client.post(self.url, {'List-Unsubscribe': 'One-Click'})
        self.assertContains(response, "You have successfully unsubscribed from")
        assert list(Optout.objects.values_list('user_id', 'course_id')) == [(self.user.id, self.course.id)]

        # Mail providers may send it more than once.
        response = self.client.post(self.url, {'List-Unsubscribe': 'One-Click'})
        assert response.status_code == 200
        assert Optout.objects.count() == 1

    def test_one_click_post_urlencoded(self):
        """
        Ensure that the one-click POST works when form-urlencoded too (the test client sends multipart by default).
        """
        response = self.client.post(
            self.url, 'List-Unsubscribe=One-Click', content_type='application/x-www-form-urlencoded'
        )
        assert response.status_code == 200
        assert Optout.objects.filter(user=self.user, course_id=self.course.id).exists()

    def test_confirm_form_post_unsubscribes(self):
        """
        Ensure that POSTing the confirmation page's form opts the user out.
        """
        response = self.client.post(self.url, {'unsubscribe': True})
        self.assertContains(response, "You have successfully unsubscribed from")
        assert Optout.objects.filter(user=self.user, course_id=self.course.id).exists()

    @ddt.data({}, {'List-Unsubscribe': 'yes'}, {'One-Click': 'List-Unsubscribe'})
    def test_post_without_confirmation(self, data):
        """
        Ensure that a POST that confirms nothing does not opt the user out.
        """
        response = self.client.post(self.url, data)
        self.assertContains(response, "You have not been unsubscribed from")
        assert Optout.objects.count() == 0

    def test_bad_tokens_are_rejected(self):
        """
        Ensure that tokens that were not signed by get_unsubscribed_link, or were edited, are rejected.
        """
        good_token = self._sign([self.user.id, str(self.course.id)])
        other_token = self._sign([self.other_user.id, str(self.course.id)])
        with override_settings(SECRET_KEY="another secret key"):
            other_key_token = self._sign([self.other_user.id, str(self.course.id)])
        bad_tokens = [
            "not-a-token",
            good_token + "x",
            # someone else's user id with this user's signature
            other_token.split(':')[0] + ':' + good_token.split(':')[1],
            self._sign([self.other_user.id, str(self.course.id)], salt='another salt'),
            other_key_token,
        ]
        for token in bad_tokens:
            for method in (self.client.get, self.client.post):
                with self.subTest(token=token, method=method.__name__):
                    response = method(self._token_url(token), {'List-Unsubscribe': 'One-Click'})
                    assert response.status_code == 404
        assert Optout.objects.count() == 0

    @ddt.data(
        (999999, None),
        (None, 'course-v1:edX+NoSuch+Course'),
        (None, 'not a course key'),
    )
    @ddt.unpack
    def test_unknown_user_or_course(self, user_id, course_id):
        """
        Ensure that a properly signed token for a user or course that doesn't exist gives a 404.
        """
        token = self._sign([user_id or self.user.id, course_id or str(self.course.id)])
        response = self.client.post(self._token_url(token), {'List-Unsubscribe': 'One-Click'})
        assert response.status_code == 404
        assert Optout.objects.count() == 0

    def test_legacy_link_does_not_allow_one_click(self):
        """
        Ensure that the old encrypted-username links still require the CSRF-protected confirmation form.
        """
        legacy_url = reverse('bulk_email_opt_out', args=[UsernameCipher.encrypt('testuser1'), str(self.course.id)])
        response = self.client.post(legacy_url, {'List-Unsubscribe': 'One-Click'})
        assert response.status_code == 403
        assert Optout.objects.count() == 0
