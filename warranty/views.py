from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from checklists.access_control import system_admin_required
from warranty.forms import WarrantyClaimUpdateForm, WarrantyWorkItemForm
from warranty.models import WarrantyClaim, WarrantyWorkItem
from warranty.services import update_claim


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
