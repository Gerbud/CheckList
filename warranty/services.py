from django.db import transaction
from django.utils import timezone

from warranty.models import WarrantyBitrixOutbox, WarrantyClaim, WarrantyHistoryEvent, WarrantyTelegramThread


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

    if locked.source == 'bitrix':
        WarrantyBitrixOutbox.objects.create(
            claim=locked,
            payload={
                'UF_STATUS': {
                    WarrantyClaim.Status.NEW: '1',
                    WarrantyClaim.Status.SERVICE_DECISION: '2',
                    WarrantyClaim.Status.CLOSED: '3',
                    WarrantyClaim.Status.IN_PROGRESS: '4',
                    WarrantyClaim.Status.CUSTOMER_WAIT: '5',
                }.get(locked.status, locked.source_status or '1'),
                'UF_COMMENT': locked.comment,
            },
        )

    if old_status != locked.status:
        WarrantyHistoryEvent.objects.create(
            claim=locked,
            kind=WarrantyHistoryEvent.Kind.CHANGE,
            actor_name=actor.get_username(),
            text=f'Статус: {dict(WarrantyClaim.Status.choices).get(old_status, old_status)} → {locked.get_status_display()}',
        )
        thread, _ = WarrantyTelegramThread.objects.get_or_create(
            claim=locked,
            defaults={'title': f'Гарантия #{locked.external_id}: {locked.product_name}'[:255]},
        )
        if locked.status == WarrantyClaim.Status.CLOSED:
            thread.state = (
                WarrantyTelegramThread.State.CLOSE_PENDING
                if thread.topic_id else WarrantyTelegramThread.State.ARCHIVED
            )
            thread.archived_at = timezone.now() if not thread.topic_id else None
        elif old_status == WarrantyClaim.Status.CLOSED:
            thread.state = WarrantyTelegramThread.State.RESTORE_PENDING
            thread.archived_at = None
        thread.save()
    return locked
