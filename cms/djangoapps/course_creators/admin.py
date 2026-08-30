"""
django admin page for the course creators table
"""


import logging
from smtplib import SMTPException

from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from cms.djangoapps.course_creators.models import (
    CourseCreator,
    CourseCreatorAllowlist,
    send_admin_notification,
    send_user_notification,
    update_creator_state
)
from cms.djangoapps.course_creators.views import (
    redeem_course_creator_allowlist_for_email,
    update_course_creator_group,
    update_org_content_creator_role
)
from common.djangoapps.edxmako.shortcuts import render_to_string

log = logging.getLogger("studio.coursecreatoradmin")


def get_email(obj):
    """ Returns the email address for a user """
    return obj.user.email

get_email.short_description = 'email'


class CourseCreatorForm(forms.ModelForm):
    """
    Admin form for course creator
    """
    class Meta:
        model = CourseCreator
        fields = '__all__'

    def clean(self):
        """
        Validate the 'state', 'organizations' and 'all_orgs' field before saving.
        """
        all_orgs = self.cleaned_data.get("all_organizations")
        orgs = self.cleaned_data.get("organizations").exists()
        state = self.cleaned_data.get("state")
        is_all_org_selected_with_orgs = (orgs and all_orgs)
        is_orgs_added_with_all_orgs_selected = (not orgs and not all_orgs)
        is_state_granted = state == CourseCreator.GRANTED
        if is_state_granted:
            if is_all_org_selected_with_orgs:
                raise ValidationError(
                    "The role can be granted either to ALL organizations or to "
                    "specific organizations but not both."
                )
            if is_orgs_added_with_all_orgs_selected:
                raise ValidationError(
                    "Specific organizations needs to be selected to grant this role,"
                    "if it is not granted to all organiztions"
                )


class CourseCreatorAdmin(admin.ModelAdmin):
    """
    Admin for the course creator table.
    """

    # Fields to display on the overview page.
    list_display = ['username', get_email, 'state', 'state_changed', 'note', 'all_organizations']
    filter_horizontal = ('organizations',)
    readonly_fields = ['username', 'state_changed']
    # Controls the order on the edit form (without this, read-only fields appear at the end).
    fieldsets = (
        (None, {
            'fields': ['username', 'state', 'state_changed', 'note', 'all_organizations', 'organizations']
        }),
    )
    # Fields that filtering support
    list_filter = ['state', 'state_changed']
    # Fields that search supports.
    search_fields = ['user__username', 'user__email', 'state', 'note']
    # Turn off the action bar (we have no bulk actions)
    actions = None
    form = CourseCreatorForm

    def username(self, inst):
        """
        Returns the username for a given user.

        Implemented to make sorting by username instead of by user object.
        """
        return inst.user.username

    username.admin_order_field = 'user__username'

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_staff

    def save_model(self, request, obj, form, change):
        # Store who is making the request.
        obj.admin = request.user
        obj.save()

    # This functions is overriden to update the m2m query
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        state = form.instance.state
        if state != CourseCreator.GRANTED:
            form.instance.organizations.clear()


class CourseCreatorAllowlistAdmin(admin.ModelAdmin):
    """
    Admin for pre-approving email addresses for course creation.

    Adding an email address here grants course creation rights to whoever owns it,
    without them having to request access first -- and works even if that person
    has not registered an account yet.
    """

    list_display = ['email', 'status', 'account', 'note', 'created', 'approved_by']
    search_fields = ['email', 'note']
    list_filter = [('redeemed_at', admin.EmptyFieldListFilter)]
    ordering = ['-created']
    readonly_fields = ['created', 'created_by', 'redeemed_at', 'redeemed_user']
    fieldsets = (
        (None, {
            'fields': ['email', 'note', 'created', 'created_by', 'redeemed_at', 'redeemed_user'],
            'description': (
                'Email addresses listed here are granted course creation rights automatically. '
                'The account does not have to exist yet: if it does, the rights are granted as soon '
                'as this entry is saved, and if it does not, they are granted the first time the '
                'newly registered user opens Studio. Each entry is used once -- redeeming it fills '
                'in the two "redeemed" fields below, and later changes made on the Course creator '
                'page are not undone by it.'
            ),
        }),
    )
    # Turn off the action bar (we have no bulk actions).
    actions = None

    def status(self, inst):
        """Human readable redemption state, for the overview page."""
        return 'granted' if inst.redeemed_at else 'awaiting registration'

    status.admin_order_field = 'redeemed_at'

    def account(self, inst):
        """Username of the account that redeemed this pre-approval, if any."""
        return inst.redeemed_user.username if inst.redeemed_user else ''

    account.admin_order_field = 'redeemed_user__username'
    account.short_description = 'redeemed by'

    def approved_by(self, inst):
        """Username of the staff member who added this pre-approval."""
        return inst.created_by.username if inst.created_by else ''

    approved_by.admin_order_field = 'created_by__username'

    def has_add_permission(self, request):
        return request.user.is_staff

    def has_change_permission(self, request, obj=None):
        return request.user.is_staff

    def has_delete_permission(self, request, obj=None):
        return request.user.is_staff

    def save_model(self, request, obj, form, change):
        # Record who approved the address; that user is the one on whose behalf
        # course creation rights are later granted.
        if obj.created_by_id is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

        # If the account already exists there is no reason to make them wait for
        # their next visit to Studio.
        if obj.redeemed_at is None and redeem_course_creator_allowlist_for_email(obj.email):
            obj.refresh_from_db()
            self.message_user(
                request,
                f'{obj.email} is already registered, so course creation rights were granted now.'
            )


admin.site.register(CourseCreator, CourseCreatorAdmin)
admin.site.register(CourseCreatorAllowlist, CourseCreatorAllowlistAdmin)


@receiver(update_creator_state, sender=CourseCreator)
def update_creator_group_callback(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Callback for when the model's creator status has changed.
    """
    user = kwargs['user']
    updated_state = kwargs['state']
    all_orgs = kwargs['all_organizations']
    create_role = all_orgs and (updated_state == CourseCreator.GRANTED)
    update_course_creator_group(kwargs['caller'], user, create_role)


@receiver(send_user_notification, sender=CourseCreator)
def send_user_notification_callback(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Callback for notifying user about course creator status change.
    """
    user = kwargs['user']
    updated_state = kwargs['state']

    studio_request_email = settings.FEATURES.get('STUDIO_REQUEST_EMAIL', '')
    context = {'studio_request_email': studio_request_email}

    if updated_state == CourseCreator.GRANTED:
        message_template = 'emails/course_creator_granted.txt'
    elif updated_state == CourseCreator.DENIED:
        message_template = 'emails/course_creator_denied.txt'
    else:
        # changed to unrequested or pending
        message_template = 'emails/course_creator_revoked.txt'

    # Rendering is inside the try as well as sending: these templates need a
    # request context (for the site name), so they cannot be rendered outside a
    # request. Failing to notify the user must never undo the status change that
    # has already been written -- and the status can legitimately be changed from
    # outside a request, for example when a pre-approved email address is
    # redeemed.
    try:
        subject = render_to_string('emails/course_creator_subject.txt', context)
        subject = ''.join(subject.splitlines())
        message = render_to_string(message_template, context)
        user.email_user(subject, message, studio_request_email)
    except:  # lint-amnesty, pylint: disable=bare-except
        log.warning("Unable to send course creator status e-mail to %s", user.email)


@receiver(send_admin_notification, sender=CourseCreator)
def send_admin_notification_callback(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Callback for notifying admin of a user in the 'pending' state.
    """
    user = kwargs['user']

    studio_request_email = settings.FEATURES.get('STUDIO_REQUEST_EMAIL', '')
    context = {'user_name': user.username, 'user_email': user.email}

    subject = render_to_string('emails/course_creator_admin_subject.txt', context)
    subject = ''.join(subject.splitlines())
    message = render_to_string('emails/course_creator_admin_user_pending.txt', context)

    try:
        send_mail(
            subject,
            message,
            studio_request_email,
            [studio_request_email],
            fail_silently=False
        )
    except SMTPException:
        log.warning("Failure sending 'pending state' e-mail for %s to %s", user.email, studio_request_email)


@receiver(m2m_changed, sender=CourseCreator.organizations.through)
def course_creator_organizations_changed_callback(sender, **kwargs):  # pylint: disable=unused-argument
    """
    Callback for addition and removal of orgs field.
    """
    instance = kwargs["instance"]
    action = kwargs["action"]
    orgs = list(instance.organizations.all().values_list('short_name', flat=True))
    updated_state = instance.state
    is_granted = updated_state == CourseCreator.GRANTED
    should_update_role = (
        (action in ["post_add", "post_remove"] and is_granted) or
        (action == "post_clear" and not is_granted)
    )
    if should_update_role:
        update_org_content_creator_role(instance.admin, instance.user, orgs)
