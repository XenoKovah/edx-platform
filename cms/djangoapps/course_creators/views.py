"""
Methods for interacting programmatically with the user creator table.
"""


import logging

from django.contrib.auth.models import User  # lint-amnesty, pylint: disable=imported-auth-user
from django.db import transaction
from django.utils import timezone

from cms.djangoapps.course_creators.models import CourseCreator, CourseCreatorAllowlist
from common.djangoapps.student import auth
from common.djangoapps.student.roles import CourseCreatorRole, OrgContentCreatorRole

log = logging.getLogger("studio.coursecreatoradmin")


def add_user_with_status_unrequested(user):
    """
    Adds a user to the course creator table with status 'unrequested'.

    If the user is already in the table, this method is a no-op
    (state will not be changed).

    If the user is marked as is_staff, this method is a no-op (user
    will not be added to table).
    """
    _add_user(user, CourseCreator.UNREQUESTED)


def add_user_with_status_granted(caller, user):
    """
    Adds a user to the course creator table with status 'granted'.

    If appropriate, this method also adds the user to the course creator group maintained by authz.py.
    Caller must have staff permissions.

    If the user is already in the table, this method is a no-op
    (state will not be changed).

    If the user is marked as is_staff, this method is a no-op (user
    will not be added to table, nor added to authz.py group).
    """
    if _add_user(user, CourseCreator.GRANTED):
        update_course_creator_group(caller, user, True)


def update_course_creator_group(caller, user, add):
    """
    Method for adding and removing users from the creator group.

    Caller must have staff permissions.
    """
    if add:
        auth.add_users(caller, CourseCreatorRole(), user)
    else:
        auth.remove_users(caller, CourseCreatorRole(), user)


def update_org_content_creator_role(caller, user, orgs):
    """
    Method for updating users for OrgContentCreatorRole, this method
    takes care of creating and deleting the role as required.

    Caller must have staff permissions.
    """
    auth.update_org_role(caller, OrgContentCreatorRole, user, orgs)


def get_course_creator_status(user):
    """
    Returns the status for a particular user, or None if user is not in the table.

    Possible return values are:
        'unrequested' = user has not requested course creation rights
        'pending' = user has requested course creation rights
        'granted' = user has been granted course creation rights
        'denied' = user has been denied course creation rights
        None = user does not exist in the table

    If the user is not already granted, any unredeemed CourseCreatorAllowlist
    entry for their email address is redeemed first (see
    `redeem_course_creator_allowlist`), so the value returned already reflects
    an administrator's pre-approval.
    """
    entry = CourseCreator.objects.filter(user=user).first()
    if entry is None or entry.state != CourseCreator.GRANTED:
        # This user's email address may have been pre-approved for course creation
        # (possibly before the account existed at all). Honor that now, so a
        # pre-approved user is never shown the "request access" prompt.
        if redeem_course_creator_allowlist(user):
            entry = CourseCreator.objects.filter(user=user).first()

    if entry is None:
        return None
    return entry.state


def user_requested_access(user):
    """
    User has requested course creator access.

    This changes the user state to CourseCreator.PENDING, unless the user
    state is already CourseCreator.GRANTED, in which case this method is a no-op.
    """
    user = CourseCreator.objects.get(user=user)
    if user.state != CourseCreator.GRANTED:
        user.state = CourseCreator.PENDING
        user.save()


def _add_user(user, state):
    """
    Adds a user to the course creator table with the specified state.

    Returns True if user was added to table, else False.

    If the user is already in the table, this method is a no-op
    (state will not be changed, method will return False).

    If the user is marked as is_staff, this method is a no-op (False will be returned).
    """
    if not user.is_staff and CourseCreator.objects.filter(user=user).count() == 0:
        entry = CourseCreator(user=user, state=state)
        entry.save()
        return True

    return False


def redeem_course_creator_allowlist(user):
    """
    Grants course creation rights to `user` if their email address was pre-approved.

    An administrator can add an email address to the CourseCreatorAllowlist table
    before the corresponding account exists. This method looks for an unredeemed
    entry matching `user.email` and, if one is found, moves the user to the
    'granted' state (for all organizations) and records the redemption.

    Returns True if rights were granted, else False.

    This is a no-op (returning False) if:
      * the user is anonymous, inactive, or has no email address -- an inactive
        user cannot be added to the course creator group, so redemption is
        deferred until the account is activated;
      * there is no unredeemed allowlist entry for the email address;
      * no staff user is available to grant the role on (see `_grant_caller`).

    Users who are already `is_staff` implicitly have course creation rights, so
    their entry is simply marked as redeemed.
    """
    if user is None or not user.is_authenticated or not user.is_active:
        return False

    email = (user.email or '').strip()
    if not email:
        return False

    entry = CourseCreatorAllowlist.objects.filter(email__iexact=email, redeemed_at__isnull=True).first()
    if entry is None:
        return False

    if user.is_staff:
        # Staff already have course creation rights and are deliberately kept out
        # of the course creator table, so just retire the pre-approval.
        _mark_allowlist_redeemed(entry, user)
        return False

    caller = _grant_caller(entry)
    if caller is None:
        log.error(
            "Cannot redeem course creator pre-approval for %s: no active staff user available to grant it.",
            email,
        )
        return False

    try:
        # All or nothing: a half-applied grant would leave the user with rights
        # while the entry still reads "awaiting registration".
        with transaction.atomic():
            _grant_all_organizations(caller, user)
            _mark_allowlist_redeemed(entry, user)
    except Exception:  # pylint: disable=broad-except
        # This runs while rendering Studio pages, so never let a failure here take
        # the page down; the entry stays unredeemed and will be retried.
        log.exception("Failed to redeem course creator pre-approval for %s.", email)
        return False

    log.info(
        "Granted course creation rights to %s from a pre-approved email address (approved by %s).",
        user.username, getattr(caller, 'username', caller),
    )
    return True


def redeem_course_creator_allowlist_for_email(email):
    """
    Redeems the pre-approval for `email` if an account with that address already exists.

    Used when an allowlist entry is added for someone who has already registered,
    so that they do not have to wait until their next visit to Studio.

    Returns True if rights were granted, else False.
    """
    email = (email or '').strip()
    if not email:
        return False

    users = list(User.objects.filter(email__iexact=email)[:2])
    if len(users) != 1:
        # No account yet (the normal case for a pre-approval), or -- rarely -- more
        # than one account shares the address, in which case we will not guess.
        if len(users) > 1:
            log.warning("Not redeeming course creator pre-approval: %s matches multiple accounts.", email)
        return False

    return redeem_course_creator_allowlist(users[0])


def _grant_all_organizations(caller, user):
    """
    Puts `user` into the 'granted' state for all organizations, creating their
    course creator table entry if needed.

    Unlike `add_user_with_status_granted`, this also upgrades a user who is
    already in the table (for example in the 'unrequested' state because they
    have visited Studio before). Adding to the course creator group is handled
    by the post_save receiver on CourseCreator, which needs `caller` stored on
    the instance as `admin`.
    """
    entry, _created = CourseCreator.objects.get_or_create(user=user)

    if entry.state == CourseCreator.GRANTED:
        # Already a course creator, possibly scoped to particular organizations by
        # an administrator; leave that configuration alone. The row can still be
        # correct while the authz group entry is missing (for instance if the user
        # was inactive when it was written), so make sure of that much.
        if entry.all_organizations:
            update_course_creator_group(caller, user, True)
        return

    entry.admin = caller
    entry.state = CourseCreator.GRANTED
    entry.all_organizations = True
    if not entry.note:
        entry.note = "Granted automatically from a pre-approved email address."
    entry.save()
    update_course_creator_group(caller, user, True)


def _mark_allowlist_redeemed(entry, user):
    """
    Records that `entry` has been used up by `user`.
    """
    entry.redeemed_at = timezone.now()
    entry.redeemed_user = user
    entry.save(update_fields=['redeemed_at', 'redeemed_user'])


def _grant_caller(entry):
    """
    Returns the staff user on whose behalf a pre-approval is granted, or None.

    Manipulating the course creator group requires a caller with global staff
    permissions. We prefer the administrator who created the allowlist entry, so
    the grant is attributed to them, and fall back to a superuser if that account
    is gone or is no longer staff.
    """
    caller = entry.created_by
    if caller is not None and caller.is_active and caller.is_staff:
        return caller

    return (
        User.objects.filter(is_active=True, is_superuser=True).order_by('id').first() or
        User.objects.filter(is_active=True, is_staff=True).order_by('id').first()
    )
