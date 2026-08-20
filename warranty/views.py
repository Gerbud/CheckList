from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from checklists.access_control import system_admin_required
from warranty.bitrix_sync import BitrixSyncError, BitrixWarrantyClient, synchronize
from warranty.forms import WarrantyClaimUpdateForm, WarrantyWorkItemForm
from warranty.models import WarrantyBitrixOutbox, WarrantyBitrixSyncState, WarrantyClaim, WarrantyWorkItem
from warranty.services import update_claim


@system_admin_required
def bitrix_settings(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'check':
            try:
                result = BitrixWarrantyClient().call('health')
            except BitrixSyncError as exc:
                messages.error(request, str(exc))
            else:
                version = result.get('version') or 'не указана'
                messages.success(request, f'Bitrix отвечает. Версия модуля: {version}.')
        elif action == 'sync':
            try:
                result = synchronize()
            except BitrixSyncError as exc:
                state = WarrantyBitrixSyncState.get_solo()
                state.last_error = str(exc)[:2000]
                state.save(update_fields=('last_error', 'updated_at'))
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    'Синхронизация завершена: получено {imported}, отправлено {sent}, ошибок {errors}.'.format(**result),
                )
        else:
            return HttpResponseForbidden('Неизвестное действие.')
        return redirect('warranty:bitrix_settings')

    state = WarrantyBitrixSyncState.get_solo()
    outbox_counts = {
        row['status']: row['total']
        for row in WarrantyBitrixOutbox.objects.values('status').annotate(total=Count('id'))
    }
    sync_url = settings.BITRIX_WARRANTY_SYNC_URL
    return render(request, 'warranty/bitrix_settings.html', {
        'portal': 'system_admin',
        'state': state,
        'sync_url': sync_url,
        'url_configured': bool(sync_url),
        'secret_configured': bool(settings.BITRIX_WARRANTY_SYNC_SECRET),
        'timeout': settings.BITRIX_WARRANTY_SYNC_TIMEOUT,
        'pending_count': outbox_counts.get(WarrantyBitrixOutbox.Status.PENDING, 0),
        'error_count': outbox_counts.get(WarrantyBitrixOutbox.Status.ERROR, 0),
        'sent_count': outbox_counts.get(WarrantyBitrixOutbox.Status.SENT, 0),
    })


@system_admin_required
def claim_list(request):
    query = WarrantyClaim.objects.select_related('assigned_to').prefetch_related('attachments')
    search = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    if search:
        filters = Q(customer_name__icontains=search) | Q(phone__icontains=search) | Q(product_name__icontains=search) | Q(serial_number__icontains=search)
        if search.isdigit():
            filters |= Q(external_id=int(search))
        query = query.filter(filters)
    if status:
        query = query.filter(status=status)
    paginator = Paginator(query, 50)
    return render(request, 'warranty/claim_list.html', {
        'portal': 'system_admin', 'claims': paginator.get_page(request.GET.get('page')),
        'statuses': WarrantyClaim.Status.choices, 'selected_status': status, 'search': search,
        'counts': {key: WarrantyClaim.objects.filter(status=key).count() for key, _ in WarrantyClaim.Status.choices},
    })


@system_admin_required
def claim_detail(request, claim_id):
    claim = get_object_or_404(
        WarrantyClaim.objects.select_related('assigned_to').prefetch_related('attachments', 'history', 'work_items', 'telegram_thread__messages'),
        pk=claim_id,
    )
    form = WarrantyClaimUpdateForm(instance=claim)
    return render(request, 'warranty/claim_detail.html', {
        'portal': 'system_admin', 'claim': claim, 'form': form, 'work_form': WarrantyWorkItemForm(),
    })


@require_POST
@system_admin_required
def claim_update(request, claim_id):
    claim = get_object_or_404(WarrantyClaim, pk=claim_id)
    form = WarrantyClaimUpdateForm(request.POST, instance=claim)
    if form.is_valid():
        update_claim(claim=claim, form=form, actor=request.user)
        messages.success(request, 'Обращение обновлено.')
    else:
        messages.error(request, 'Проверьте заполнение формы.')
    return redirect('warranty:claim_detail', claim_id=claim.pk)


@require_POST
@system_admin_required
def work_item_add(request, claim_id):
    claim = get_object_or_404(WarrantyClaim, pk=claim_id)
    form = WarrantyWorkItemForm(request.POST)
    if form.is_valid():
        WarrantyWorkItem.objects.create(claim=claim, **form.cleaned_data)
        messages.success(request, 'Позиция добавлена.')
    else:
        messages.error(request, 'Не удалось добавить позицию.')
    return redirect('warranty:claim_detail', claim_id=claim.pk)
