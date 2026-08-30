"""
Table for storing information about whether or not Studio users have course creation privileges.
"""

from django.contrib.auth.models import User  # lint-amnesty, pylint: disable=imported-auth-user
from django.db import models
from django.db.models.signals import post_init, post_save
from django.dispatch import Signal, receiver
from django.utils import timezone

from django.utils.translation import gettext_lazy as _
from organizations.models import Organization

# A signal that will be sent when users should be added or removed from the creator group
# providing_args=["caller", "user", "state", "organizations"]
update_creator_state = Signal()

# A signal that will be sent when admin should be notified of a pending user request
# providing_args=["user"]
send_admin_notification = Signal()

# A signal that will be sent when user should be notified of change in course creator privileges
# providing_args=["user", "state"]
send_user_notification = Signal()


class CourseCreator(models.Model):
    """
    Creates the database table model.

    .. no_pii:
    """
    UNREQUESTED = 'unrequested'
    PENDING = 'pending'
    GRANTED = 'granted'
    DENIED = 'denied'

    # Second value is the "human-readable" version.
    STATES = (
        (UNREQUESTED, _('unrequested')),
        (PENDING, _('pending')),
        (GRANTED, _('granted')),
        (DENIED, _('denied')),
    )

    user = models.OneToOneField(User, help_text=_("Studio user"), on_delete=models.CASCADE)
    state_changed = models.DateTimeField('state last updated', auto_now_add=True,
                                         help_text=_("The date when state was last updated"))
    state = models.CharField(max_length=24, blank=False, choices=STATES, default=UNREQUESTED,
                             help_text=_("Current course creator state"))
    note = models.CharField(max_length=512, blank=True, help_text=_("Optional notes about this user (for example, "
                                                                    "why course creation access was denied)"))
    organizations = models.ManyToManyField(Organization, blank=True,
                                           help_text=_("Organizations under which the user is allowed "
                                                       "to create courses."))
    all_organizations = models.BooleanField(default=True,
                                            help_text=_("Grant the user the permission to create courses "
                                                        "in ALL organizations"))

    def __str__(self):
        return f"{self.user} | {self.state} [{self.state_changed}]"


@receiver(post_init, sender=CourseCreator)
def post_init_callback(sender, **kwargs):  # lint-amnesty, pylint: disable=unused-argument
    """
    Extend to store previous state.
    """
    instance = kwargs['instance']
    # Only read fields that are actually loaded. Reading a deferred field here
    # calls refresh_from_db(), which builds another instance deferring a
    # *different* set of fields and fires post_init again, ping-ponging between
    # state and all_organizations until the stack is exhausted. Django defers
    # fields on the querysets it uses to cascade deletes, so this is reachable
    # just by deleting a user who is in the course creator table.
    if instance.get_deferred_fields():
        return
    instance.orig_state = instance.state
    instance.orig_all_organizations = instance.all_organizations


@receiver(post_save, sender=CourseCreator)
def post_save_callback(sender, **kwargs):
    """
    Extend to update state_changed time and fire event to update course creator group, if appropriate.
    """
    instance = kwargs['instance']
    if not hasattr(instance, 'orig_state'):
        # Partially loaded instance (see post_init_callback): there is no baseline
        # to compare against, so there is no state transition to react to.
        return
    # We only wish to modify the state_changed time if the state has been modified. We don't wish to
    # modify it for changes to the notes field.
    # We need to keep track of all_organization switch. If this switch is changed we are going to remove the
    # Course Creator group.
    if instance.state != instance.orig_state or instance.all_organizations != instance.orig_all_organizations:
        granted_state_change = instance.state == CourseCreator.GRANTED or instance.orig_state == CourseCreator.GRANTED  # pylint: disable=consider-using-in
        # If either old or new state is 'granted', we must manipulate the course creator
        # group maintained by authz. That requires staff permissions (stored admin).
        if granted_state_change:
            assert hasattr(instance, 'admin'), 'Must have stored staff user to change course creator group'
            update_creator_state.send(
                sender=sender,
                caller=instance.admin,
                user=instance.user,
                state=instance.state,
                all_organizations=instance.all_organizations
            )

        # If user has been denied access, granted access, or previously granted access has been
        # revoked, send a notification message to the user.
        if instance.state == CourseCreator.DENIED or granted_state_change:
            send_user_notification.send(
                sender=sender,
                user=instance.user,
                state=instance.state
            )

        # If the user has gone into the 'pending' state, send a notification to interested admin.
        if instance.state == CourseCreator.PENDING:
            send_admin_notification.send(
                sender=sender,
                user=instance.user
            )

        instance.state_changed = timezone.now()
        instance.orig_state = instance.state
        instance.orig_all_organizations = instance.all_organizations
        instance.save()


class CourseCreatorAllowlist(models.Model):
    """
    Pre-approves an email address for Studio course creation rights.

    A row may be added before the matching account exists, which is the point:
    an administrator can approve an incoming instructor by email address, and
    that instructor never has to click "Request the Ability to Create Courses".

    The entry is *redeemed* the first time Studio looks up the course creator
    status of the account owning that email address -- a CourseCreator record is
    created (or upgraded) with state 'granted', which in turn adds the user to
    the course creator group.  Redemption is recorded on the row so it happens
    at most once; later changes made on the CourseCreator admin page are not
    undone by a stale pre-approval.

    .. pii: Stores an email address supplied by an administrator so that course
        creation rights can be granted before the account is registered.
    .. pii_types: email_address
    .. pii_retirement: retained
    """
    email = models.EmailField(
        unique=True,
        help_text=_("Email address to pre-approve for course creation. "
                    "The account does not have to exist yet.")
    )
    note = models.CharField(
        max_length=512,
        blank=True,
        help_text=_("Optional notes about this pre-approval (for example, who asked for it).")
    )
    created = models.DateTimeField(
        auto_now_add=True,
        help_text=_("The date when this email address was pre-approved")
    )
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        help_text=_("The staff user who pre-approved this email address. Course creation rights "
                    "are granted on their behalf.")
    )
    redeemed_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        help_text=_("The date when course creation rights were actually granted. Empty until an "
                    "account with this email address registers and opens Studio.")
    )
    redeemed_user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        help_text=_("The account that redeemed this pre-approval.")
    )

    class Meta:
        verbose_name = _("pre-approved course creator email")
        verbose_name_plural = _("pre-approved course creator emails")

    def __str__(self):
        if self.redeemed_at:
            return f"{self.email} | granted [{self.redeemed_at}]"
        return f"{self.email} | awaiting registration"

    def clean(self):
        """
        Normalize the email address so lookups do not depend on database collation.
        """
        super().clean()
        if self.email:
            self.email = self.email.strip().lower()

    def save(self, *args, **kwargs):  # pylint: disable=signature-differs
        # Normalize here as well as in clean() so that rows created outside the
        # admin form (shell, management commands) are stored the same way.
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)
