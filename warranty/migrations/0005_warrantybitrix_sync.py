from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('warranty', '0004_alter_warrantytelegramthread_state')]

    operations = [
        migrations.CreateModel(
            name='WarrantyBitrixSyncState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('claim_cursor', models.PositiveBigIntegerField(default=0)),
                ('history_cursor', models.PositiveBigIntegerField(default=0)),
                ('last_success_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='WarrantyBitrixOutbox',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('payload', models.JSONField(default=dict)),
                ('status', models.CharField(choices=[('pending', 'Ожидает отправки'), ('sending', 'Отправляется'), ('sent', 'Отправлено'), ('error', 'Ошибка')], default='pending', max_length=16)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('claim', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bitrix_outbox', to='warranty.warrantyclaim')),
            ],
            options={'ordering': ('id',)},
        ),
        migrations.AddIndex(
            model_name='warrantybitrixoutbox',
            index=models.Index(fields=['status', 'id'], name='warranty_wa_status_4f6892_idx'),
        ),
    ]
