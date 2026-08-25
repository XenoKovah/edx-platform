"""
Register the `exclude_completed` "don't send to" option as a valid Target type.

This only widens the `choices` on `Target.target_type`; the underlying column is unchanged.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bulk_email', '0007_disabledcourse'),
    ]

    operations = [
        migrations.AlterField(
            model_name='target',
            name='target_type',
            field=models.CharField(
                choices=[
                    ('myself', 'Myself'),
                    ('staff', 'Staff and instructors'),
                    ('learners', 'All students'),
                    ('cohort', 'Specific cohort'),
                    ('track', 'Specific course mode'),
                    ('exclude_completed', 'Excluding students who completed the course'),
                ],
                max_length=64,
            ),
        ),
    ]
