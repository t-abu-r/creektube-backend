from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('media', '0013_video_public_ids_snip_thumbnail'),
        ('accounts', '0009_profile_recovery_email_securitycode'),
    ]

    operations = [
        migrations.AddField(
            model_name='video',
            name='visibility',
            field=models.CharField(choices=[('public', 'Public'), ('unlisted', 'Unlisted'), ('private', 'Private')], default='public', max_length=10),
        ),
        migrations.AddField(
            model_name='snip',
            name='visibility',
            field=models.CharField(choices=[('public', 'Public'), ('unlisted', 'Unlisted'), ('private', 'Private')], default='public', max_length=10),
        ),
    ]
