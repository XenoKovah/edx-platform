"""
Adds the table backing pre-approval of course creators by email address.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('course_creators', '0002_add_org_support_for_course_creators'),
    ]

    operations = [
        migrations.CreateModel(
            name='CourseCreatorAllowlist',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(help_text='Email address to pre-approve for course creation. The account does not have to exist yet.', max_length=254, unique=True)),
                ('note', models.CharField(blank=True, help_text='Optional notes about this pre-approval (for example, who asked for it).', max_length=512)),
                ('created', models.DateTimeField(auto_now_add=True, help_text='The date when this email address was pre-approved')),
                ('redeemed_at', models.DateTimeField(blank=True, default=None, help_text='The date when course creation rights were actually granted. Empty until an account with this email address registers and opens Studio.', null=True)),
                ('created_by', models.ForeignKey(blank=True, help_text='The staff user who pre-approved this email address. Course creation rights are granted on their behalf.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('redeemed_user', models.ForeignKey(blank=True, help_text='The account that redeemed this pre-approval.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'pre-approved course creator email',
                'verbose_name_plural': 'pre-approved course creator emails',
            },
        ),
    ]
