"""
Bulk Email Data

This provides Data models to represent Bulk Email data.
"""


class BulkEmailTargetChoices:
    """
    Enum for the available targets (recipient groups) of an email authored with the bulk course email tool.

    SEND_TO_MYSELF      - Message intended for author of the message
    SEND_TO_STAFF       - Message intended for all course staff
    SEND_TO_LEARNERS    - Message intended for all enrolled learners
    SEND_TO_COHORT      - Message intended for a specific cohort
    SEND_TO_TRACK       - Message intended for all learners in a specific track (e.g. audit or verified)

    Exclusion targets subtract from, rather than add to, the recipient list. They are stored alongside the
    "send to" targets on a CourseEmail so that they survive scheduling and editing, but they never contribute
    recipients of their own.

    EXCLUDE_COMPLETED   - Suppress learners who have already earned a course certificate
    """
    SEND_TO_MYSELF = "myself"
    SEND_TO_STAFF = "staff"
    SEND_TO_LEARNERS = "learners"
    SEND_TO_COHORT = "cohort"
    SEND_TO_TRACK = "track"
    EXCLUDE_COMPLETED = "exclude_completed"

    # Targets that add recipients to a message.
    INCLUSION_TARGET_CHOICES = (SEND_TO_MYSELF, SEND_TO_STAFF, SEND_TO_LEARNERS, SEND_TO_COHORT, SEND_TO_TRACK)
    # Targets that remove recipients from a message.
    EXCLUSION_TARGET_CHOICES = (EXCLUDE_COMPLETED,)

    TARGET_CHOICES = INCLUSION_TARGET_CHOICES + EXCLUSION_TARGET_CHOICES

    @classmethod
    def is_valid_target(cls, target):
        """
        Given the target of a message, return a boolean indicating whether the target choice is valid.
        """
        return target in cls.TARGET_CHOICES

    @classmethod
    def is_exclusion_target(cls, target):
        """
        Given the target of a message, return a boolean indicating whether the target removes recipients from the
        message instead of adding them.
        """
        return target in cls.EXCLUSION_TARGET_CHOICES
