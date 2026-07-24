from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('media', '0011_snip_sniplike'),
    ]

    operations = [
        migrations.AddField(
            model_name='comment',
            name='snip',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='comments',
                to='media.snip',
            ),
        ),
    ]
