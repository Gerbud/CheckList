"""Application signal handlers."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver


User = get_user_model()


@receiver(pre_save, sender=User)
def protect_main_administrator_on_save(sender, instance, **kwargs):
    if not instance.pk:
        return
    previous = sender.objects.filter(pk=instance.pk).values(
        'username',
        'is_active',
    ).first()
    if not previous or previous['username'].casefold() != 'bud':
        return
    if instance.username.casefold() != 'bud':
        raise ValidationError('Имя главного администратора Bud менять нельзя.')
    if previous['is_active'] and not instance.is_active:
        raise ValidationError('Главного администратора Bud отключить нельзя.')


@receiver(pre_delete, sender=User)
def protect_main_administrator_on_delete(sender, instance, **kwargs):
    if instance.username.casefold() == 'bud':
        raise ValidationError('Главного администратора Bud удалить нельзя.')
