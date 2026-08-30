"""
Tests pre-approving course creators by email address.
"""


from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import TestCase
from django.test.client import RequestFactory

from cms.djangoapps.course_creators.admin import CourseCreatorAllowlistAdmin
from cms.djangoapps.course_creators.models import CourseCreator, CourseCreatorAllowlist
from cms.djangoapps.course_creators.views import (
    add_user_with_status_unrequested,
    get_course_creator_status,
    redeem_course_creator_allowlist,
    redeem_course_creator_allowlist_for_email
)
from common.djangoapps.student import auth
from common.djangoapps.student.roles import CourseCreatorRole
from common.djangoapps.student.tests.factories import UserFactory


@mock.patch.dict('django.conf.settings.FEATURES', {'ENABLE_CREATOR_GROUP': True})
class CourseCreatorAllowlistTest(TestCase):
    """
    Tests that an email address can be granted course creation rights before the
    account behind it exists.
    """

    def setUp(self):
        super().setUp()
        self.admin = UserFactory.create(
            username='Mark',
            email='admin+courses@edx.org',
            password='foo',
            is_staff=True,
        )

    def _pre_approve(self, email, created_by=None, note=''):
        """ Adds an allowlist entry the way the admin page would. """
        return CourseCreatorAllowlist.objects.create(
            email=email,
            note=note,
            created_by=self.admin if created_by is None else created_by,
        )

    def _register(self, email, **kwargs):
        """ Creates the account that the pre-approval was written for. """
        return UserFactory.create(username='new_instructor', email=email, password='foo', **kwargs)

    def test_pre_approved_email_is_granted_on_first_lookup(self):
        entry = self._pre_approve('instructor@example.com')
        user = self._register('instructor@example.com')

        self.assertFalse(auth.user_has_role(user, CourseCreatorRole()))
        self.assertEqual('granted', get_course_creator_status(user))
        self.assertTrue(auth.user_has_role(user, CourseCreatorRole()))
        self.assertTrue(CourseCreator.objects.get(user=user).all_organizations)

        entry.refresh_from_db()
        self.assertIsNotNone(entry.redeemed_at)
        self.assertEqual(user, entry.redeemed_user)

    def test_email_match_ignores_case_and_whitespace(self):
        self._pre_approve('  Instructor@Example.COM ')
        user = self._register('instructor@example.com')
        self.assertEqual('granted', get_course_creator_status(user))

    def test_status_is_unchanged_without_a_pre_approval(self):
        user = self._register('nobody@example.com')
        self.assertIsNone(get_course_creator_status(user))
        add_user_with_status_unrequested(user)
        self.assertEqual('unrequested', get_course_creator_status(user))
        self.assertFalse(auth.user_has_role(user, CourseCreatorRole()))

    def test_existing_unrequested_user_is_upgraded(self):
        user = self._register('instructor@example.com')
        add_user_with_status_unrequested(user)
        self.assertEqual('unrequested', get_course_creator_status(user))

        self._pre_approve('instructor@example.com')
        self.assertEqual('granted', get_course_creator_status(user))
        self.assertTrue(auth.user_has_role(user, CourseCreatorRole()))

    def test_entry_is_redeemed_only_once(self):
        entry = self._pre_approve('instructor@example.com')
        user = self._register('instructor@example.com')
        self.assertEqual('granted', get_course_creator_status(user))

        # An administrator revoking access on the course creator page must stick.
        record = CourseCreator.objects.get(user=user)
        record.admin = self.admin
        record.state = CourseCreator.DENIED
        record.save()

        self.assertEqual('denied', get_course_creator_status(user))
        entry.refresh_from_db()
        self.assertEqual(user, entry.redeemed_user)

    def test_inactive_user_defers_redemption(self):
        entry = self._pre_approve('instructor@example.com')
        user = self._register('instructor@example.com', is_active=False)

        # An inactive user cannot be added to the course creator group, so nothing
        # is granted and the pre-approval is left for the next attempt.
        self.assertIsNone(get_course_creator_status(user))
        entry.refresh_from_db()
        self.assertIsNone(entry.redeemed_at)

        user.is_active = True
        user.save()
        self.assertEqual('granted', get_course_creator_status(user))
        self.assertTrue(auth.user_has_role(user, CourseCreatorRole()))

    def test_already_registered_account_is_granted_immediately(self):
        user = self._register('instructor@example.com')
        self._pre_approve('instructor@example.com')

        self.assertTrue(redeem_course_creator_allowlist_for_email('INSTRUCTOR@example.com'))
        self.assertEqual('granted', get_course_creator_status(user))

    def test_no_account_yet_is_not_an_error(self):
        self._pre_approve('instructor@example.com')
        self.assertFalse(redeem_course_creator_allowlist_for_email('instructor@example.com'))

    def test_falls_back_to_a_superuser_when_approver_is_not_staff(self):
        superuser = UserFactory.create(
            username='root', email='root@edx.org', password='foo', is_staff=True, is_superuser=True,
        )
        non_staff = UserFactory.create(username='helper', email='helper@edx.org', password='foo')
        self._pre_approve('instructor@example.com', created_by=non_staff)
        user = self._register('instructor@example.com')

        self.assertEqual('granted', get_course_creator_status(user))
        self.assertTrue(auth.user_has_role(user, CourseCreatorRole()))
        self.assertTrue(superuser.is_superuser)

    def test_no_staff_user_available_defers_redemption(self):
        self.admin.is_staff = False
        self.admin.save()
        entry = self._pre_approve('instructor@example.com', created_by=self.admin)
        user = self._register('instructor@example.com')

        self.assertIsNone(get_course_creator_status(user))
        entry.refresh_from_db()
        self.assertIsNone(entry.redeemed_at)

    def test_staff_account_retires_the_entry(self):
        entry = self._pre_approve('instructor@example.com')
        user = self._register('instructor@example.com', is_staff=True)

        # Staff already have course creation rights and are kept out of the table.
        self.assertFalse(redeem_course_creator_allowlist(user))
        self.assertFalse(CourseCreator.objects.filter(user=user).exists())
        entry.refresh_from_db()
        self.assertIsNotNone(entry.redeemed_at)

    def test_admin_records_the_approver_and_grants_now(self):
        user = self._register('instructor@example.com')
        request = RequestFactory().post('/admin/course_creators/coursecreatorallowlist/add/')
        request.user = self.admin
        request.session = {}
        request._messages = FallbackStorage(request)  # pylint: disable=protected-access

        model_admin = CourseCreatorAllowlistAdmin(CourseCreatorAllowlist, AdminSite())
        entry = CourseCreatorAllowlist(email='Instructor@example.com')
        model_admin.save_model(request, entry, None, False)

        entry.refresh_from_db()
        self.assertEqual('instructor@example.com', entry.email)
        self.assertEqual(self.admin, entry.created_by)
        self.assertEqual(user, entry.redeemed_user)
        self.assertEqual('granted', get_course_creator_status(user))
