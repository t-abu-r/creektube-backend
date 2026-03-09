from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('media', '0004_alter_video_thumbnail_alter_video_video'),  # keep your existing dependency name
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        # Safe - skips if column already exists
        migrations.RunSQL(
            sql="ALTER TABLE media_mediaprofile ADD COLUMN IF NOT EXISTS official boolean NOT NULL DEFAULT false;",
            reverse_sql="ALTER TABLE media_mediaprofile DROP COLUMN IF EXISTS official;",
        ),
        migrations.CreateModel(
            name='Creek',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, null=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='account', to='media.mediaprofile')),
                ('author', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='auth.user')),
            ],
        ),
        migrations.CreateModel(
            name='DisPike',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, null=True)),
                ('author', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='auth.user')),
                ('video', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='dispikes', to='media.video')),
            ],
        ),
    ]