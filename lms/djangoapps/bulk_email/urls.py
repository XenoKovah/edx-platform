"""
URLs for bulk_email app
"""

from django.conf import settings
from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        r'^email/optout/(?P<token>[a-zA-Z0-9-_=]+)/{}/$'.format(
            settings.COURSE_ID_PATTERN,
        ),
        views.opt_out_email_updates,
        name='bulk_email_opt_out',
    ),
    re_path(
        r'^email/unsubscribe/(?P<token>[a-zA-Z0-9-_:]+)/$',
        views.unsubscribe_from_course_email,
        name='bulk_email_unsubscribe',
    ),
    re_path(
        r'^email/site/unsubscribe/(?P<token>[a-zA-Z0-9-_:]+)/$',
        views.unsubscribe_from_site_email,
        name='bulk_email_site_email_unsubscribe',
    ),
]
