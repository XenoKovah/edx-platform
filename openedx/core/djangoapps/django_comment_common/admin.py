"""
Admin for managing the connection to the Forums backend service.
"""


from django.contrib import admin

from .models import ForumShadowMute, ForumsConfig

admin.site.register(ForumsConfig)


@admin.register(ForumShadowMute)
class ForumShadowMuteAdmin(admin.ModelAdmin):
    """
    OST2: manage per-course forum shadow mutes.

    Moderators normally apply these from the post's "..." menu in the
    Discussions MFE; this page is the audit trail and the way to lift a mute
    for a learner whose posts you can no longer conveniently find.
    """
    list_display = ('user', 'course_id', 'is_active', 'created', 'created_by')
    list_filter = ('is_active', 'course_id')
    search_fields = ('user__username', 'user__email', 'course_id', 'reason')
    raw_id_fields = ('user', 'created_by')
    readonly_fields = ('created', 'modified')
