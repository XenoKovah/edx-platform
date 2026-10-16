"""
Tests for the site-wide email preference.
"""

import pytest

from common.djangoapps.student.tests.factories import UserFactory
from openedx.core.djangolib.testing.utils import CacheIsolationTestCase, skip_unless_lms

from ...errors import PreferenceValidationError
from ..api import set_user_preference, update_user_preferences
from ..email_preference import (
    EMAIL_PREFERENCE_ALL,
    EMAIL_PREFERENCE_COURSE,
    EMAIL_PREFERENCE_KEY,
    EMAIL_PREFERENCE_NONE,
    get_email_preference,
    get_user_ids_with_email_preference,
    set_email_preference
)


@skip_unless_lms
class TestEmailPreference(CacheIsolationTestCase):
    """
    Tests for reading, writing and validating the site-wide email preference.
    """

    def setUp(self):
        super().setUp()
        self.user = UserFactory.create()

    def test_default_is_all(self):
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_ALL

    def test_set_and_get(self):
        for value in (EMAIL_PREFERENCE_COURSE, EMAIL_PREFERENCE_NONE, EMAIL_PREFERENCE_ALL):
            set_email_preference(self.user, value)
            assert get_email_preference(self.user) == value

    def test_set_rejects_unknown_values(self):
        with pytest.raises(ValueError):
            set_email_preference(self.user, 'sometimes')

    def test_user_ids_with_email_preference(self):
        course_user = UserFactory.create()
        none_user = UserFactory.create()
        set_email_preference(course_user, EMAIL_PREFERENCE_COURSE)
        set_email_preference(none_user, EMAIL_PREFERENCE_NONE)
        assert set(get_user_ids_with_email_preference(EMAIL_PREFERENCE_NONE)) == {none_user.id}
        assert set(get_user_ids_with_email_preference(EMAIL_PREFERENCE_COURSE, EMAIL_PREFERENCE_NONE)) == {
            course_user.id, none_user.id,
        }

    def test_preferences_api_accepts_known_values(self):
        set_user_preference(self.user, EMAIL_PREFERENCE_KEY, EMAIL_PREFERENCE_NONE)
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_NONE
        update_user_preferences(self.user, {EMAIL_PREFERENCE_KEY: EMAIL_PREFERENCE_COURSE})
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_COURSE

    def test_preferences_api_rejects_unknown_values(self):
        for update in (
            lambda: set_user_preference(self.user, EMAIL_PREFERENCE_KEY, 'sometimes'),
            lambda: update_user_preferences(self.user, {EMAIL_PREFERENCE_KEY: 'sometimes'}),
        ):
            with pytest.raises(PreferenceValidationError) as context_manager:
                update()
            assert list(context_manager.value.preference_errors) == [EMAIL_PREFERENCE_KEY]
        assert get_email_preference(self.user) == EMAIL_PREFERENCE_ALL
