from django.db import transaction
from django.utils import timezone

from warranty.models import WarrantyBitrixOutbox, WarrantyClaim, WarrantyHistoryEvent, WarrantyTelegramThread


BITRIX_STATUS_BY_LOCAL = {
    WarrantyClaim.Status.NEW: '1',
    WarrantyClaim.Status.SERVICE_DECISION: '2',
    WarrantyClaim.Status.CLOSED: '3',
    WarrantyClaim.Status.IN_PROGRESS: '4',
    WarrantyClaim.Status.CUSTOMER_WAIT: '5',
}


def _queue_bitrix_update(claim):
    if claim.source == 'bitrix':
        WarrantyBitrixOutbox.objects.create(
            claim=claim,
            payload={
                'UF_STATUS': BITRIX_STATUS_BY_LOCAL.get(claim.status, claim.source_status or '1'),
                'UF_COMMENT': claim.comment,
            },
        )


def _record_status_change(claim, old_status, actor_name):
    WarrantyHistoryEvent.objects.create(
        claim=claim,
        kind=WarrantyHistoryEvent.Kind.CHANGE,
        actor_name=actor_name,
        text=f'Статус: {dict(WarrantyClaim.Status.choices).get(old_status, old_status)} → {claim.get_status_display()}',
    )
    thread, _ = WarrantyTelegramThread.objects.get_or_create(
        claim=claim,
        defaults={'title': f'Гарантия #{claim.external_id}: {claim.product_name}'[:255]},
    )
    if claim.status == WarrantyClaim.Status.CLOSED:
        thread.state = (
            WarrantyTelegramThread.State.CLOSE_PENDING
            if thread.topic_id else WarrantyTelegramThread.State.ARCHIVED
        )
        thread.archived_at = timezone.now() if not thread.topic_id else None
    elif old_status == WarrantyClaim.Status.CLOSED:
        thread.state = WarrantyTelegramThread.State.RESTORE_PENDING
        thread.archived_at = None
    elif thread.topic_id:
        thread.state = WarrantyTelegramThread.State.STATUS_UPDATE_PENDING
    thread.save()


@transaction.atomic
def update_claim(*, claim, form, actor):
    locked = WarrantyClaim.objects.select_for_update().get(pk=claim.pk)
    old_status = locked.status
    updated = form.save(commit=False)
    locked.status = updated.status
    locked.priority = updated.priority
    locked.assigned_to = updated.assigned_to
    locked.due_at = updated.due_at
    locked.comment = updated.comment
    locked.save()

    _queue_bitrix_update(locked)

    if old_status != locked.status:
        _record_status_change(locked, old_status, actor.get_username())
    return locked


@transaction.atomic
def apply_telegram_status_button(*, claim_id, button, actor_name):
    locked = WarrantyClaim.objects.select_for_update().get(pk=claim_id)
    if not button.is_enabled or locked.status != button.source_status:
        return locked, False
    old_status = locked.status
    locked.status = button.target_status
    locked.save(update_fields=('status', 'closed_at', 'updated_at'))
    _queue_bitrix_update(locked)
    _record_status_change(locked, old_status, actor_name)
    return locked, True
