"""
Tests for certificate generation handler
"""
import logging
from unittest import mock

import ddt
from django.conf import settings
from django.test import override_settings
from edx_toggles.toggles.testutils import override_waffle_flag

from common.djangoapps.student.tests.factories import CourseEnrollmentFactory, UserFactory
from lms.djangoapps.certificates.generation_handler import (
    CERTIFICATES_USE_ALLOWLIST,
    CERTIFICATES_USE_UPDATED,
    is_using_certificate_allowlist,
    _is_using_v2_course_certificates,
    _can_generate_allowlist_certificate,
    _can_generate_certificate_for_status,
    _can_generate_v2_certificate,
    can_generate_certificate_task,
    generate_allowlist_certificate_task,
    generate_certificate_task,
    generate_regular_certificate_task,
    is_using_certificate_allowlist_and_is_on_allowlist
)
from lms.djangoapps.certificates.models import CertificateStatuses, GeneratedCertificate
from lms.djangoapps.certificates.tests.factories import (
    CertificateInvalidationFactory,
    CertificateWhitelistFactory,
    GeneratedCertificateFactory
)
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory

log = logging.getLogger(__name__)

BETA_TESTER_METHOD = 'lms.djangoapps.certificates.generation_handler._is_beta_tester'
CCX_COURSE_METHOD = 'lms.djangoapps.certificates.generation_handler._is_ccx_course'
INTEGRITY_ENABLED_METHOD = 'lms.djangoapps.certificates.generation_handler.is_integrity_signature_enabled'
ID_VERIFIED_METHOD = 'lms.djangoapps.verify_student.services.IDVerificationService.user_is_verified'
PASSING_GRADE_METHOD = 'lms.djangoapps.certificates.generation_handler._has_passing_grade'
WEB_CERTS_METHOD = 'lms.djangoapps.certificates.generation_handler.has_html_certificates_enabled'


@mock.patch(INTEGRITY_ENABLED_METHOD, mock.Mock(return_value=False))
@override_waffle_flag(CERTIFICATES_USE_ALLOWLIST, active=True)
@mock.patch(ID_VERIFIED_METHOD, mock.Mock(return_value=True))
@mock.patch(WEB_CERTS_METHOD, mock.Mock(return_value=True))
@ddt.ddt
class AllowlistTests(ModuleStoreTestCase):
    """
    Tests for handling allowlist certificates
    """

    def setUp(self):
        super().setUp()

        # Create user, a course run, and an enrollment
        self.user = UserFactory()
        self.course_run = CourseFactory()
        self.course_run_key = self.course_run.id  # pylint: disable=no-member
        self.enrollment = CourseEnrollmentFactory(
            user=self.user,
            course_id=self.course_run_key,
            is_active=True,
            mode="verified",
        )

        # Whitelist user
        CertificateWhitelistFactory.create(course_id=self.course_run_key, user=self.user)

    def test_is_using_allowlist_true(self):
        """
        Test the allowlist flag
        """
        assert is_using_certificate_allowlist(self.course_run_key)

    @override_waffle_flag(CERTIFICATES_USE_ALLOWLIST, active=False)
    def test_is_using_allowlist_false(self):
        """
        Test the allowlist flag without the override
        """
        assert not is_using_certificate_allowlist(self.course_run_key)

    def test_is_using_allowlist_and_is_on_list(self):
        """
        Test the allowlist flag and the presence of the user on the list
        """
        assert is_using_certificate_allowlist_and_is_on_allowlist(self.user, self.course_run_key)

    @override_waffle_flag(CERTIFICATES_USE_ALLOWLIST, active=False)
    def test_is_using_allowlist_and_is_on_list_with_flag_off(self):
        """
        Test the allowlist flag and the presence of the user on the list when the flag is off
        """
        assert not is_using_certificate_allowlist_and_is_on_allowlist(self.user, self.course_run_key)

    def test_is_using_allowlist_and_is_on_list_true(self):
        """
        Test the allowlist flag and the presence of the user on the list when the user is not on the list
        """
        u = UserFactory()
        CourseEnrollmentFactory(
            user=u,
            course_id=self.course_run_key,
            is_active=True,
            mode="verified",
        )
        CertificateWhitelistFactory.create(course_id=self.course_run_key, user=u, whitelist=False)
        assert not is_using_certificate_allowlist_and_is_on_allowlist(u, self.course_run_key)

    @ddt.data(
        (CertificateStatuses.deleted, True),
        (CertificateStatuses.deleting, True),
        (CertificateStatuses.downloadable, False),
        (CertificateStatuses.error, True),
        (CertificateStatuses.generating, True),
        (CertificateStatuses.notpassing, True),
        (CertificateStatuses.restricted, True),
        (CertificateStatuses.unavailable, True),
        (CertificateStatuses.audit_passing, True),
        (CertificateStatuses.audit_notpassing, True),
        (CertificateStatuses.honor_passing, True),
        (CertificateStatuses.unverified, True),
        (CertificateStatuses.invalidated, True),
        (CertificateStatuses.requesting, True))
    @ddt.unpack
    def test_generation_status(self, status, expected_response):
        """
        Test handling of certificate statuses
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        GeneratedCertificateFactory(
            user=u,
            course_id=key,
            mode=GeneratedCertificate.MODES.verified,
            status=status,
        )

        assert _can_generate_certificate_for_status(u, key) == expected_response

    def test_generation_status_for_none(self):
        """
        Test handling of certificate statuses for a non-existent cert
        """
        assert _can_generate_certificate_for_status(None, None)

    @override_waffle_flag(CERTIFICATES_USE_ALLOWLIST, active=False)
    def test_handle_invalid(self):
        """
        Test handling of an invalid user/course run combo
        """
        assert not _can_generate_allowlist_certificate(self.user, self.course_run_key)
        assert not generate_allowlist_certificate_task(self.user, self.course_run_key)
        assert not can_generate_certificate_task(self.user, self.course_run_key)
        assert not generate_certificate_task(self.user, self.course_run_key)

    def test_handle_valid(self):
        """
        Test handling of a valid user/course run combo
        """
        assert _can_generate_allowlist_certificate(self.user, self.course_run_key)
        assert generate_allowlist_certificate_task(self.user, self.course_run_key)

    def test_handle_valid_general_methods(self):
        """
        Test handling of a valid user/course run combo for the general (non-allowlist) generation methods
        """
        assert can_generate_certificate_task(self.user, self.course_run_key)
        assert generate_certificate_task(self.user, self.course_run_key)

#    def test_can_generate_not_verified(self):
    @ddt.data(False, True)
    def test_can_generate_not_verified(self, idv_retired):
        """
        Test handling when the user's id is not verified
        """
#        with mock.patch(ID_VERIFIED_METHOD, return_value=False):
#            assert not _can_generate_allowlist_certificate(self.user, self.course_run_key)
        with mock.patch(ID_VERIFIED_METHOD, return_value=False), \
                mock.patch(INTEGRITY_ENABLED_METHOD, return_value=idv_retired):
            self.assertEqual(idv_retired,
                             _can_generate_allowlist_certificate(self.user, self.course_run_key, self.enrollment_mode))
            self.assertIsNot(idv_retired,
                             _set_allowlist_cert_status(
                                 self.user, self.course_run_key,
                                 self.enrollment_mode, self.grade) == CertificateStatuses.unverified)

    def test_can_generate_not_enrolled(self):
        """
        Test handling when user is not enrolled
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        CertificateWhitelistFactory.create(course_id=key, user=u)
        assert not _can_generate_allowlist_certificate(u, key)

    def test_can_generate_audit(self):
        """
        Test handling when user is enrolled in audit mode
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        CourseEnrollmentFactory(
            user=u,
            course_id=key,
            is_active=True,
            mode="audit",
        )
        CertificateWhitelistFactory.create(course_id=key, user=u)

        assert not _can_generate_allowlist_certificate(u, key)

    def test_can_generate_not_whitelisted(self):
        """
        Test handling when user is not whitelisted
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        CourseEnrollmentFactory(
            user=u,
            course_id=key,
            is_active=True,
            mode="verified",
        )
        assert not _can_generate_allowlist_certificate(u, key)

    def test_can_generate_invalidated(self):
        """
        Test handling when user is on the invalidate list
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        CourseEnrollmentFactory(
            user=u,
            course_id=key,
            is_active=True,
            mode="verified",
        )
        cert = GeneratedCertificateFactory(
            user=u,
            course_id=key,
            mode=GeneratedCertificate.MODES.verified,
            status=CertificateStatuses.downloadable
        )
        CertificateWhitelistFactory.create(course_id=key, user=u)
        CertificateInvalidationFactory.create(
            generated_certificate=cert,
            invalidated_by=self.user,
            active=True
        )

        assert not _can_generate_allowlist_certificate(u, key)

    def test_can_generate_web_cert_disabled(self):
        """
        Test handling when web certs are not enabled
        """
        with mock.patch(WEB_CERTS_METHOD, return_value=False):
            assert not _can_generate_allowlist_certificate(self.user, self.course_run_key)


    def test_generate_allowlist_honor_cert(self):
        """
        Test that verifies we can generate an Honor cert for an Open edX installation configured to support Honor
        certificates.
        """
        course_run = CourseFactory()
        course_run_key = course_run.id  # pylint: disable=no-member
        enrollment_mode = CourseMode.HONOR
        CourseEnrollmentFactory(
            user=self.user,
            course_id=course_run_key,
            is_active=True,
            mode=enrollment_mode,
        )

        CertificateAllowlistFactory.create(course_id=course_run_key, user=self.user)

        # Enable Honor Certificates and verify we can generate an AllowList certificate
        with override_settings(FEATURES={**settings.FEATURES, 'DISABLE_HONOR_CERTIFICATES': False}):
            assert _can_generate_allowlist_certificate(self.user, course_run_key, enrollment_mode)

        # Disable Honor Certificates and verify we cannot generate an AllowList certificate
        with override_settings(FEATURES={**settings.FEATURES, 'DISABLE_HONOR_CERTIFICATES': True}):
            assert not _can_generate_allowlist_certificate(self.user, course_run_key, enrollment_mode)

@mock.patch(INTEGRITY_ENABLED_METHOD, mock.Mock(return_value=False))
@override_waffle_flag(CERTIFICATES_USE_UPDATED, active=True)
@mock.patch(ID_VERIFIED_METHOD, mock.Mock(return_value=True))
@mock.patch(CCX_COURSE_METHOD, mock.Mock(return_value=False))
@mock.patch(PASSING_GRADE_METHOD, mock.Mock(return_value=True))
@mock.patch(WEB_CERTS_METHOD, mock.Mock(return_value=True))
@ddt.ddt
class CertificateTests(ModuleStoreTestCase):
    """
    Tests for handling course certificates
    """

    def setUp(self):
        super().setUp()

        # Create user, a course run, and an enrollment
        self.user = UserFactory()
        self.course_run = CourseFactory()
        self.course_run_key = self.course_run.id  # pylint: disable=no-member
        self.enrollment = CourseEnrollmentFactory(
            user=self.user,
            course_id=self.course_run_key,
            is_active=True,
            mode="verified",
        )

    def test_handle_valid(self):
        """
        Test handling of a valid user/course run combo.
        """
        assert _can_generate_v2_certificate(self.user, self.course_run_key)
        assert can_generate_certificate_task(self.user, self.course_run_key)
        assert generate_certificate_task(self.user, self.course_run_key)

    def test_handle_valid_task(self):
        """
        Test handling of a valid user/course run combo.

        We test generate_certificate_task() and generate_regular_certificate_task() separately since they both
        generate a cert.
        """
        assert generate_regular_certificate_task(self.user, self.course_run_key)

    @override_waffle_flag(CERTIFICATES_USE_UPDATED, active=False)
    def test_handle_invalid(self):
        """
        Test handling of an invalid user/course run combo
        """
        assert not _can_generate_v2_certificate(self.user, self.course_run_key)
        assert not can_generate_certificate_task(self.user, self.course_run_key)
        assert not generate_certificate_task(self.user, self.course_run_key)
        assert not generate_regular_certificate_task(self.user, self.course_run_key)

    def test_is_using_updated_true(self):
        """
        Test the updated flag
        """
        assert _is_using_v2_course_certificates(self.course_run_key)

    @override_waffle_flag(CERTIFICATES_USE_UPDATED, active=False)
    def test_is_using_updated_false(self):
        """
        Test the updated flag without the override
        """
        assert not _is_using_v2_course_certificates(self.course_run_key)

    @ddt.data(
        (CertificateStatuses.deleted, True),
        (CertificateStatuses.deleting, True),
        (CertificateStatuses.downloadable, False),
        (CertificateStatuses.error, True),
        (CertificateStatuses.generating, True),
        (CertificateStatuses.notpassing, True),
        (CertificateStatuses.restricted, True),
        (CertificateStatuses.unavailable, True),
        (CertificateStatuses.audit_passing, True),
        (CertificateStatuses.audit_notpassing, True),
        (CertificateStatuses.honor_passing, True),
        (CertificateStatuses.unverified, True),
        (CertificateStatuses.invalidated, True),
        (CertificateStatuses.requesting, True))
    @ddt.unpack
    def test_generation_status(self, status, expected_response):
        """
        Test handling of certificate statuses
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        GeneratedCertificateFactory(
            user=u,
            course_id=key,
            mode=GeneratedCertificate.MODES.verified,
            status=status,
        )

        assert _can_generate_certificate_for_status(u, key) == expected_response

    def test_generation_status_for_none(self):
        """
        Test handling of certificate statuses for a non-existent cert
        """
        assert _can_generate_certificate_for_status(None, None)

    def test_can_generate_not_verified(self):
        """
        Test handling when the user's id is not verified
        """
        with mock.patch(ID_VERIFIED_METHOD, return_value=False):
            assert not _can_generate_v2_certificate(self.user, self.course_run_key)

    def test_can_generate_ccx(self):
        """
        Test handling when the course is a CCX (custom edX) course
        """
        with mock.patch(CCX_COURSE_METHOD, return_value=True):
            assert not _can_generate_v2_certificate(self.user, self.course_run_key)

    def test_can_generate_beta_tester(self):
        """
        Test handling when the user is a beta tester
        """
        with mock.patch(BETA_TESTER_METHOD, return_value=True):
            assert not _can_generate_v2_certificate(self.user, self.course_run_key)

    def test_can_generate_failing_grade(self):
        """
        Test handling when the user does not have a passing grade
        """
        with mock.patch(PASSING_GRADE_METHOD, return_value=False):
            assert not _can_generate_v2_certificate(self.user, self.course_run_key)

    def test_can_generate_not_enrolled(self):
        """
        Test handling when user is not enrolled
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        assert not _can_generate_v2_certificate(u, key)

    def test_can_generate_audit(self):
        """
        Test handling when user is enrolled in audit mode
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        CourseEnrollmentFactory(
            user=u,
            course_id=key,
            is_active=True,
            mode="audit",
        )

        assert not _can_generate_v2_certificate(u, key)

    def test_can_generate_invalidated(self):
        """
        Test handling when user is on the invalidate list
        """
        u = UserFactory()
        cr = CourseFactory()
        key = cr.id  # pylint: disable=no-member
        CourseEnrollmentFactory(
            user=u,
            course_id=key,
            is_active=True,
            mode="verified",
        )
        cert = GeneratedCertificateFactory(
            user=u,
            course_id=key,
            mode=GeneratedCertificate.MODES.verified,
            status=CertificateStatuses.downloadable
        )
        CertificateInvalidationFactory.create(
            generated_certificate=cert,
            invalidated_by=self.user,
            active=True
        )

        assert not _can_generate_v2_certificate(u, key)

    def test_can_generate_web_cert_disabled(self):
        """
        Test handling when web certs are not enabled
        """
        with mock.patch(WEB_CERTS_METHOD, return_value=False):
            assert not _can_generate_v2_certificate(self.user, self.course_run_key)
