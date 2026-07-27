from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('media', '0014_video_snip_visibility'),
    ]

    operations = [
        # Wipe all existing watch events (old system)
        migrations.RunSQL(
            sql="DELETE FROM media_watchevent;",
            reverse_sql="-- no reverse",
        ),
        # Wipe all existing view counts
        migrations.RunSQL(
            sql="UPDATE media_video SET view_count = 0;",
            reverse_sql="-- no reverse",
        ),
        migrations.RunSQL(
            sql="UPDATE media_snip SET view_count = 0;",
            reverse_sql="-- no reverse",
        ),
        # Make video nullable on WatchEvent
        migrations.AlterField(
            model_name='watchevent',
            name='video',
            field=models.ForeignKey(
                related_name='watch_events',
                to='media.video',
                on_delete=django.db.models.deletion.CASCADE,
                null=True,
                blank=True,
            ),
        ),
        # Add snip FK to WatchEvent
        migrations.AddField(
            model_name='watchevent',
            name='snip',
            field=models.ForeignKey(
                related_name='watch_events',
                to='media.snip',
                on_delete=django.db.models.deletion.CASCADE,
                null=True,
                blank=True,
            ),
        ),
        # Add new indexes
        migrations.AddIndex(
            model_name='watchevent',
            index=models.Index(fields=['snip', 'timestamp'], name='media_watche_snip_ti_idx'),
        ),
        migrations.AddIndex(
            model_name='watchevent',
            index=models.Index(fields=['user', 'snip'], name='media_watche_user_snip_idx'),
        ),
    ]
