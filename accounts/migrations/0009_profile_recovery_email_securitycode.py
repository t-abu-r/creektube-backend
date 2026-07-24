from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_profile_notification_reply'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='recovery_email',
            field=models.EmailField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='profile',
            name='recovery_email_verified',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='SecurityCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=6)),
                ('purpose', models.CharField(choices=[('password_change', 'Password Change Verification'), ('password_reset', 'Password Reset'), ('recovery_email', 'Recovery Email Verification')], max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField()),
                ('used', models.BooleanField(default=False)),
                ('user', models.ForeignKey(on_delete=models.CASCADE, related_name='security_codes', to='auth.user')),
            ],
            options={
                'indexes': [models.Index(fields=['user', 'purpose', 'used'], name='accounts_sec_user_purpo_idx')],
            },
        ),
    ]
