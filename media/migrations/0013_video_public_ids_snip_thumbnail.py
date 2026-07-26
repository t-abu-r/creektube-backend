from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('media', '0012_comment_snip'),
    ]

    operations = [
        migrations.AddField(
            model_name='video',
            name='video_public_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='video',
            name='thumbnail_public_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AlterField(
            model_name='video',
            name='thumbnail',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='snip',
            name='thumbnail',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='snip',
            name='video_public_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='snip',
            name='thumbnail_public_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
