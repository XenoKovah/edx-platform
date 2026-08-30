from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import opaque_keys.edx.django.models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('django_comment_common', '0009_coursediscussionsettings_reported_content_email_notifications'),
    ]

    operations = [
        migrations.CreateModel(
            name='ForumShadowMute',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('course_id', opaque_keys.edx.django.models.CourseKeyField(db_index=True, max_length=255)),
                ('is_active', models.BooleanField(default=True, help_text='Uncheck to lift the mute while keeping the record of it.')),
                ('reason', models.TextField(blank=True, default='', help_text='Optional note to other moderators about why this mute was applied.')),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('modified', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, help_text='The moderator who applied the mute.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='forum_shadow_mutes_applied', to=settings.AUTH_USER_MODEL)),
                ('user', models.ForeignKey(help_text='The learner whose forum posts are hidden from their peers.', on_delete=django.db.models.deletion.CASCADE, related_name='forum_shadow_mutes', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'forum shadow mute',
                'verbose_name_plural': 'forum shadow mutes',
                'unique_together': {('user', 'course_id')},
            },
        ),
    ]
