from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('warranty', '0025_product_consultation')]

    operations = [
        migrations.CreateModel(
            name='WarrantyCustomerConsultationMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('question', models.TextField()),
                ('answer', models.TextField()),
                ('customer_message_id', models.CharField(blank=True, max_length=64)),
                ('assistant_message_id', models.CharField(blank=True, max_length=64)),
                ('support_message_id', models.CharField(blank=True, max_length=64)),
                ('shared_with_support_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consultation_messages', to='warranty.warrantycustomersession')),
            ],
        ),
    ]
