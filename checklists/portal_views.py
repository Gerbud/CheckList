import csv
import json
from datetime import date, timedelta
from io import StringIO
from urllib import parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Min, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from checklists.access_control import (
    DIRECTOR_STORE_SESSION_KEY,
    get_post_login_redirect,
    is_store_director,
    is_system_admin,
    price_tag_tool_required,
    resolve_managed_store,
    set_managed_store,
    store_director_required,
    system_admin_required,
)
from checklists.ad_hoc_tasks import (
    cancel_ad_hoc_task,
    copy_ad_hoc_task,
    create_ad_hoc_task,
    delete_ad_hoc_task,
    update_ad_hoc_task,
)
from checklists.calendar_services import (
    get_store_day_status,
    iter_month_dates,
    set_store_day_status,
)
from checklists.exceptions import ChecklistServiceError, OperationNotAllowedError
from checklists.management_services import (
    activate_checklist_question,
    activate_managed_user,
    activate_store_employee,
    bulk_create_shift_assignments,
    create_checklist_question,
    create_managed_user,
    create_shift_assignment,
    create_store_employee,
    create_store_with_defaults,
    clear_all_audit_logs,
    clear_store_audit_log,
    deactivate_checklist_question,
    deactivate_managed_user,
    deactivate_store_employee,
    delete_store_safely,
    delete_shift_assignment,
    delete_checklist_question,
    delete_managed_user,
    get_checklist_question_history,
    get_current_questions,
    get_store_deletion_summary,
    reorder_checklist_questions,
    reopen_stage_with_reason,
    remove_user_store_membership,
    reset_managed_user_password,
    send_store_test_notification,
    set_user_store_membership,
    update_checklist_question,
    update_managed_user,
    update_shift_assignment,
    update_store,
    update_store_employee,
    update_store_logo,
    update_store_notification_settings,
    update_store_schedule,
)
from checklists.models import (
    AnswerRevision,
    AuditLog,
    ChecklistAnswer,
    ChecklistDayStatus,
    ChecklistItem,
    ChecklistNotification,
    ChecklistTemplateVersion,
    DailyChecklist,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    PriceTagGeneration,
    PriceTagGenerationItem,
    PriceTagNameCorrection,
    Store,
    ShiftTemplate,
    StoreAdHocTask,
    StoreChecklistSchedule,
    StoreDayStatus,
    StoreEmployee,
    StoreNotificationSettings,
    StorePriceTagTemplate,
    StorePriceTagCategory,
    StoreTerminalAccount,
    TelegramOutboundMessage,
    TelegramUserProfile,
    UserStoreMembership,
)
from checklists.notifications import TelegramDeliveryError
from checklists.portal_forms import (
    AuditClearConfirmationForm,
    BulkShiftForm,
    ChecklistQuestionForm,
    ManagedUserCreateForm,
    ManagedUserUpdateForm,
    PasswordResetForm,
    PriceTagLinksForm,
    StorePriceTagCategoryForm,
    ReopenStageForm,
    ShiftAssignmentForm,
    ShiftCopyForm,
    ShiftTemplateForm,
    StoreCreateForm,
    StoreDayStatusForm,
    StoreEmployeeForm,
    StoreLogoForm,
    StorePriceTagTemplateForm,
    StoreAdHocTaskForm,
    StoreAdHocTaskCopyForm,
    UserStoreMembershipForm,
    StoreForm,
    StoreNotificationForm,
    StoreScheduleForm,
    TelegramTestForm,
    managed_user_initial,
)
from checklists.price_tags import (
    ProductImportError,
    apply_category_rules,
    build_qr_url,
    import_product,
    download_product_image,
    render_qr_png,
    select_product_properties,
    site_url_matches,
    suggest_product_name,
)
from checklists.shift_calendar import (
    SHIFT_CELL_META,
    copy_week_to_month,
    create_shift_template,
    delete_shift_template,
    month_completion,
    serialize_assignment,
    update_calendar_cells,
)
from checklists.reporting import (
    get_daily_report,
    get_director_dashboard_data,
    get_employee_report,
    get_revision_report,
)
from checklists.reporting_v2 import (
    build_daily_rows,
    build_employee_rows,
    build_recurring_problems,
    build_report_dashboard,
    calculate_store_health,
    collect_store_facts,
    get_revision_analytics,
    get_task_analytics,
    identify_problems,
    make_report_period,
)


def _request_metadata(request):
    return {
        'ip_address': request.META.get('REMOTE_ADDR'),
        'user_agent': request.META.get('HTTP_USER_AGENT'),
    }


def _store_timezone(store):
    try:
        return ZoneInfo(store.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo('UTC')


def _store_today(store):
    return timezone.now().astimezone(_store_timezone(store)).date()


def _parse_date(value, default=None):
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        return default


def _parse_month(value, default):
    if not value:
        return default.replace(day=1)
    try:
        return date.fromisoformat(f'{value}-01')
    except ValueError:
        return default.replace(day=1)


def _adjacent_month(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _querystring_without_page(request):
    query = request.GET.copy()
    query.pop('page', None)
    return query.urlencode()


def _service_form_error(form, exc):
    if isinstance(exc, ValidationError) and hasattr(exc, 'message_dict'):
        for field, errors in exc.message_dict.items():
            target = field if field in form.fields else None
            for error in errors:
                form.add_error(target, error)
    else:
        form.add_error(None, str(exc))


def _portal_name(request):
    return 'system_admin' if is_system_admin(request.user) else 'director'


class RoleLoginView(LoginView):
    template_name = 'registration/login.html'

    def get_default_redirect_url(self):
        return get_post_login_redirect(self.request.user)


@login_required
def post_login_redirect(request):
    target = get_post_login_redirect(request.user)
    if target == '/login/':
        return HttpResponseForbidden('Для активного доступа не настроена роль.')
    return redirect(target)


@store_director_required
def director_dashboard(request):
    store = request.current_store
    now = timezone.now()
    work_date = now.astimezone(_store_timezone(store)).date()
    data = get_director_dashboard_data(store, work_date)
    recent_revisions = get_revision_report(
        store,
        work_date - timedelta(days=7),
        work_date,
    )[:10]
    telegram_errors = ChecklistNotification.objects.filter(
        stage__daily_checklist__store=store,
        status=ChecklistNotification.Status.FAILED,
    ).select_related('stage').order_by('-updated_at')[:10]
    return render(
        request,
        'checklists/director/dashboard.html',
        {
            'portal': 'director',
            'store': store,
            'work_date': work_date,
            'now_local': now.astimezone(_store_timezone(store)),
            'recent_revisions': recent_revisions,
            'telegram_errors': telegram_errors,
            **data,
        },
    )


@store_director_required
def director_employees(request):
    store = request.current_store
    query = StoreEmployee.objects.filter(store=store)
    search = request.GET.get('q', '').strip()
    activity = request.GET.get('activity', '')
    if search:
        query = query.filter(
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(display_name__icontains=search)
            | Q(personnel_number__icontains=search)
        )
    if activity in {'active', 'inactive'}:
        query = query.filter(is_active=activity == 'active')
    page = Paginator(query.order_by('sort_order', 'display_name'), 25).get_page(
        request.GET.get('page')
    )
    return render(
        request,
        'checklists/director/employees.html',
        {
            'portal': 'director',
            'store': store,
            'page': page,
            'search': search,
            'activity': activity,
            'querystring': _querystring_without_page(request),
        },
    )


@store_director_required
def director_employee_add(request):
    store = request.current_store
    form = StoreEmployeeForm(request.POST or None, store=store)
    if request.method == 'POST' and form.is_valid():
        try:
            employee = create_store_employee(
                store,
                form.cleaned_data,
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError, IntegrityError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Сотрудник создан.')
            return redirect('checklists:director_employee_edit', employee_id=employee.pk)
    return render(
        request,
        'checklists/portal_form.html',
        {'portal': 'director', 'store': store, 'form': form, 'title': 'Новый сотрудник'},
    )


@store_director_required
def director_employee_edit(request, employee_id):
    store = request.current_store
    employee = get_object_or_404(StoreEmployee, pk=employee_id, store=store)
    form = StoreEmployeeForm(
        request.POST or None,
        instance=employee,
        store=store,
    )
    if request.method == 'POST' and form.is_valid():
        try:
            employee = update_store_employee(
                store,
                employee,
                form.cleaned_data,
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Данные сотрудника сохранены.')
            return redirect('checklists:director_employee_edit', employee_id=employee.pk)
    participation = {
        'answers': employee.answers_given.count(),
        'revisions': employee.answer_revisions.count(),
        'stages': employee.completed_stages.count(),
        'shifts': employee.shift_assignments.count(),
    }
    return render(
        request,
        'checklists/director/employee_form.html',
        {
            'portal': 'director',
            'store': store,
            'form': form,
            'employee': employee,
            'participation': participation,
        },
    )


def _employee_activity(request, employee_id, active):
    store = request.current_store
    if request.method != 'POST':
        return HttpResponseForbidden('Изменение доступно только через POST.')
    employee = get_object_or_404(StoreEmployee, pk=employee_id, store=store)
    service = activate_store_employee if active else deactivate_store_employee
    service(store, employee, request.user, _request_metadata(request))
    messages.success(request, 'Статус сотрудника изменён.')
    return redirect('checklists:director_employees')


@store_director_required
def director_employee_activate(request, employee_id):
    return _employee_activity(request, employee_id, True)


@store_director_required
def director_employee_deactivate(request, employee_id):
    return _employee_activity(request, employee_id, False)


@store_director_required
def director_shifts(request):
    store = request.current_store
    today = _store_today(store)
    selected_month = _parse_month(request.GET.get('month'), today)
    assignments = list(
        DailyShiftAssignment.objects.filter(
            store=store,
            work_date__year=selected_month.year,
            work_date__month=selected_month.month,
        ).select_related('employee').order_by(
            'work_date',
            'employee__sort_order',
            'employee__display_name',
        )
    )
    assignments_by_date = {}
    for assignment in assignments:
        assignments_by_date.setdefault(assignment.work_date, []).append(
            assignment
        )
    days = [
        {
            'date': work_date,
            'assignments': assignments_by_date.get(work_date, []),
            'day_status': get_store_day_status(store, work_date),
            'editable': work_date >= today,
        }
        for work_date in iter_month_dates(selected_month)
    ]
    return render(
        request,
        'checklists/director/shift_month.html',
        {
            'portal': 'director',
            'store': store,
            'selected_month': selected_month,
            'previous_month': _adjacent_month(selected_month, -1),
            'next_month': _adjacent_month(selected_month, 1),
            'days': days,
            'can_edit_month': selected_month >= today.replace(day=1),
            'day_off_status': ChecklistDayStatus.DAY_OFF,
        },
    )


@login_required
def employee_schedule(request):
    today = timezone.localdate()
    selected_month = _parse_month(request.GET.get('month'), today)
    employees = list(
        StoreEmployee.objects.filter(
            user=request.user,
            is_active=True,
            store__is_active=True,
        ).select_related('store')
    )
    assignments = list(
        DailyShiftAssignment.objects.filter(
            employee__in=employees,
            work_date__year=selected_month.year,
            work_date__month=selected_month.month,
        ).select_related('store', 'employee').order_by(
            'work_date',
            'shift_start',
            'store__name',
        )
    )
    assignments_by_date = {}
    for assignment in assignments:
        assignments_by_date.setdefault(assignment.work_date, []).append(
            assignment
        )
    days = [
        {
            'date': work_date,
            'assignments': assignments_by_date.get(work_date, []),
        }
        for work_date in iter_month_dates(selected_month)
    ]
    return render(
        request,
        'checklists/employee/schedule.html',
        {
            'selected_month': selected_month,
            'previous_month': _adjacent_month(selected_month, -1),
            'next_month': _adjacent_month(selected_month, 1),
            'days': days,
            'employees': employees,
            'has_assignments': bool(assignments),
        },
    )


@store_director_required
def director_shift_date(request, work_date):
    store = request.current_store
    selected = _parse_date(work_date)
    if selected is None:
        raise Http404
    assignments = DailyShiftAssignment.objects.filter(
        store=store,
        work_date=selected,
    ).select_related('employee')
    can_edit = selected >= _store_today(store)
    return render(
        request,
        'checklists/director/shifts.html',
        {
            'portal': 'director',
            'store': store,
            'work_date': selected,
            'assignments': assignments,
            'can_edit': can_edit,
            'copy_form': ShiftCopyForm(initial={'target_date': selected + timedelta(days=1)}),
        },
    )


@store_director_required
def director_shift_add(request, work_date):
    store = request.current_store
    selected = _parse_date(work_date)
    if selected is None:
        raise Http404
    form = ShiftAssignmentForm(request.POST or None, store=store)
    if request.method == 'POST' and form.is_valid():
        try:
            create_shift_assignment(
                store,
                selected,
                form.cleaned_data,
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError, IntegrityError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Назначение создано.')
            return redirect('checklists:director_shift_date', work_date=selected.isoformat())
    return render(
        request,
        'checklists/portal_form.html',
        {'portal': 'director', 'store': store, 'form': form, 'title': f'Смена {selected:%d.%m.%Y}'},
    )


@store_director_required
def director_shift_edit(request, work_date, assignment_id):
    store = request.current_store
    selected = _parse_date(work_date)
    assignment = get_object_or_404(
        DailyShiftAssignment,
        pk=assignment_id,
        store=store,
        work_date=selected,
    )
    form = ShiftAssignmentForm(request.POST or None, instance=assignment, store=store)
    if request.method == 'POST' and form.is_valid():
        try:
            update_shift_assignment(
                store,
                assignment,
                form.cleaned_data,
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError, IntegrityError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Назначение изменено.')
            return redirect('checklists:director_shift_date', work_date=selected.isoformat())
    return render(
        request,
        'checklists/portal_form.html',
        {'portal': 'director', 'store': store, 'form': form, 'title': 'Изменить смену'},
    )


@store_director_required
def director_shift_remove(request, work_date, assignment_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    store = request.current_store
    selected = _parse_date(work_date)
    assignment = get_object_or_404(
        DailyShiftAssignment,
        pk=assignment_id,
        store=store,
        work_date=selected,
    )
    try:
        delete_shift_assignment(
            store,
            assignment,
            request.user,
            _request_metadata(request),
        )
    except OperationNotAllowedError as exc:
        return HttpResponseForbidden(str(exc))
    messages.success(request, 'Ошибочное назначение удалено.')
    return redirect('checklists:director_shift_date', work_date=selected.isoformat())


@store_director_required
def director_shift_copy(request, work_date):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    store = request.current_store
    source_date = _parse_date(work_date)
    form = ShiftCopyForm(request.POST)
    if form.is_valid():
        created = skipped = 0
        try:
            for assignment in DailyShiftAssignment.objects.filter(
                store=store,
                work_date=source_date,
            ).select_related('employee'):
                if DailyShiftAssignment.objects.filter(
                    store=store,
                    employee=assignment.employee,
                    work_date=form.cleaned_data['target_date'],
                ).exists():
                    skipped += 1
                    continue
                create_shift_assignment(
                    store,
                    form.cleaned_data['target_date'],
                    {
                        'employee': assignment.employee,
                        'shift_type': assignment.shift_type,
                        'is_responsible_for_checklist': assignment.is_responsible_for_checklist,
                        'shift_start': assignment.shift_start,
                        'shift_end': assignment.shift_end,
                        'comment': assignment.comment,
                    },
                    request.user,
                    _request_metadata(request),
                )
                created += 1
        except (ChecklistServiceError, ValidationError) as exc:
            messages.error(request, str(exc))
            return redirect(
                'checklists:director_shift_date',
                work_date=source_date.isoformat(),
            )
        messages.success(request, f'Создано: {created}; пропущено: {skipped}.')
        return redirect(
            'checklists:director_shift_date',
            work_date=form.cleaned_data['target_date'].isoformat(),
        )
    messages.error(request, 'Неверная дата копирования.')
    return redirect('checklists:director_shift_date', work_date=source_date.isoformat())


@store_director_required
def director_shifts_bulk(request):
    store = request.current_store
    today = _store_today(store)
    selected_month = _parse_month(request.GET.get('month'), today)
    template_form = ShiftTemplateForm(
        request.POST if request.POST.get('action') == 'template_create' else None
    )
    if (
        request.method == 'POST'
        and request.POST.get('action') == 'template_create'
        and template_form.is_valid()
    ):
        try:
            create_shift_template(
                store=store,
                data=template_form.cleaned_data,
                actor=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError, IntegrityError) as exc:
            _service_form_error(template_form, exc)
        else:
            messages.success(request, 'Шаблон смены создан.')
            return redirect(
                f"{reverse('checklists:director_shifts_bulk')}?"
                f'month={selected_month:%Y-%m}'
            )
    elif request.method == 'POST':
        # Совместимость со старым POST массового планирования.
        legacy_form = BulkShiftForm(request.POST, store=store)
        if legacy_form.is_valid():
            try:
                result = bulk_create_shift_assignments(
                    store,
                    legacy_form.cleaned_data,
                    request.user,
                    _request_metadata(request),
                )
            except (ChecklistServiceError, ValidationError) as exc:
                _service_form_error(legacy_form, exc)
            else:
                messages.success(
                    request,
                    f"Создано: {result['created']}; "
                    f"обновлено: {result['updated']}; "
                    f"пропущено: {result['skipped']}.",
                )
                return redirect(
                    f"{reverse('checklists:director_shifts_bulk')}?"
                    f'month={selected_month:%Y-%m}'
                )
        for error in legacy_form.non_field_errors():
            messages.error(request, error)

    all_employees = list(
        StoreEmployee.objects.filter(
            store=store,
            is_active=True,
        ).order_by('sort_order', 'display_name', 'pk')
    )
    completion = month_completion(store, selected_month, all_employees)
    department = request.GET.get('department', '')
    schedule_status = request.GET.get('status', '')
    employees = all_employees
    if department in StoreEmployee.Department.values:
        employees = [
            employee
            for employee in employees
            if employee.department == department
        ]
    if schedule_status == 'filled':
        employees = [
            employee
            for employee in employees
            if employee.pk not in completion['incomplete_ids']
        ]
    elif schedule_status == 'missing':
        employees = [
            employee
            for employee in employees
            if employee.pk in completion['incomplete_ids']
        ]
    month_days = list(iter_month_dates(selected_month))
    assignments = {
        (assignment.employee_id, assignment.work_date): assignment
        for assignment in DailyShiftAssignment.objects.filter(
            store=store,
            employee__in=employees,
            work_date__year=selected_month.year,
            work_date__month=selected_month.month,
        ).select_related('employee')
    }
    rows = []
    for employee in employees:
        rows.append({
            'employee': employee,
            'is_complete': (
                employee.pk not in completion['incomplete_ids']
            ),
            'cells': [
                {
                    'date': work_date,
                    'assignment': serialize_assignment(
                        assignments.get((employee.pk, work_date))
                    ),
                    'editable': work_date >= today,
                }
                for work_date in month_days
            ],
        })
    month_end = month_days[-1]
    first_week = selected_month - timedelta(
        days=selected_month.weekday()
    )
    weeks = []
    current_week = first_week
    while current_week <= month_end:
        weeks.append(current_week)
        current_week += timedelta(days=7)
    return render(
        request,
        'checklists/director/bulk_shifts.html',
        {
            'portal': 'director',
            'store': store,
            'selected_month': selected_month,
            'previous_month': _adjacent_month(selected_month, -1),
            'next_month': _adjacent_month(selected_month, 1),
            'month_days': month_days,
            'rows': rows,
            'all_employees': all_employees,
            'completion': completion,
            'employees_without_schedule': len(
                completion['incomplete_ids']
            ),
            'department_choices': StoreEmployee.Department.choices,
            'selected_department': department,
            'selected_status': schedule_status,
            'shift_types': [
                {'value': value, **SHIFT_CELL_META[value]}
                for value in DailyShiftAssignment.ShiftType.values
            ],
            'templates': ShiftTemplate.objects.filter(
                store=store,
                is_active=True,
            ),
            'template_form': template_form,
            'weeks': weeks,
            'can_edit_month': selected_month >= today.replace(day=1),
            'default_shift_date': max(selected_month, today),
            'month_end': month_end,
        },
    )


@store_director_required
def director_shift_calendar_update(request):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse(
            {'ok': False, 'error': 'Некорректный JSON.'},
            status=400,
        )
    updates = payload.get('updates')
    if updates is None:
        updates = [payload]
    try:
        cells = update_calendar_cells(
            store=request.current_store,
            updates=updates,
            actor=request.user,
            request_metadata=_request_metadata(request),
        )
    except OperationNotAllowedError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=403)
    except (ValidationError, IntegrityError, ValueError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    month_start = date.fromisoformat(cells[0]['date']).replace(day=1)
    employees = list(
        StoreEmployee.objects.filter(
            store=request.current_store,
            is_active=True,
        )
    )
    completion = month_completion(
        request.current_store,
        month_start,
        employees,
    )
    return JsonResponse({
        'ok': True,
        'cells': cells,
        'completion_percent': completion['completion_percent'],
        'employees_without_schedule': len(completion['incomplete_ids']),
    })


@store_director_required
def director_shift_calendar_copy_week(request):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    try:
        payload = json.loads(request.body or '{}')
        month_start = _parse_month(payload.get('month'), _store_today(
            request.current_store
        ))
        week_start = date.fromisoformat(payload['week_start'])
        employee_ids = [
            int(value) for value in payload.get('employee_ids', [])
        ]
        cells = copy_week_to_month(
            store=request.current_store,
            month_start=month_start,
            week_start=week_start,
            employee_ids=employee_ids,
            actor=request.user,
            request_metadata=_request_metadata(request),
        )
    except OperationNotAllowedError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=403)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    employees = list(
        StoreEmployee.objects.filter(
            store=request.current_store,
            is_active=True,
        )
    )
    completion = month_completion(
        request.current_store,
        month_start,
        employees,
    )
    return JsonResponse({
        'ok': True,
        'changed': len(cells),
        'cells': cells,
        'completion_percent': completion['completion_percent'],
        'employees_without_schedule': len(completion['incomplete_ids']),
    })


@store_director_required
def director_shift_template_delete(request, template_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    template = get_object_or_404(
        ShiftTemplate,
        pk=template_id,
        store=request.current_store,
    )
    delete_shift_template(
        store=request.current_store,
        template=template,
        actor=request.user,
    )
    messages.success(request, 'Шаблон смены удалён.')
    return redirect('checklists:director_shifts_bulk')


@store_director_required
def director_questions(request):
    store = request.current_store
    try:
        questions = list(get_current_questions(store))
    except ChecklistServiceError as exc:
        questions = []
        messages.error(request, str(exc))
    grouped = []
    for code, label in DailyChecklistStage.SectionCode.choices:
        grouped.append((code, label, [q for q in questions if q.section.code == code]))
    return render(
        request,
        'checklists/director/questions.html',
        {'portal': 'director', 'store': store, 'grouped_questions': grouped},
    )


@store_director_required
def director_question_add(request):
    store = request.current_store
    form = ChecklistQuestionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            create_checklist_question(store, form.cleaned_data, request.user, _request_metadata(request))
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Вопрос создан в новой версии шаблона.')
            return redirect('checklists:director_questions')
    return render(request, 'checklists/portal_form.html', {'portal': 'director', 'store': store, 'form': form, 'title': 'Новый вопрос'})


@store_director_required
def director_question_edit(request, question_id):
    store = request.current_store
    question = get_object_or_404(
        ChecklistItem.objects.select_related('section'),
        pk=question_id,
        section__version__template__store=store,
        section__version__status=ChecklistTemplateVersion.Status.PUBLISHED,
    )
    form = ChecklistQuestionForm(
        request.POST or None,
        initial=ChecklistQuestionForm.initial_from_item(question),
    )
    if request.method == 'POST' and form.is_valid():
        try:
            update_checklist_question(store, question, form.cleaned_data, request.user, _request_metadata(request))
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Новая версия вопроса опубликована.')
            return redirect('checklists:director_questions')
    return render(request, 'checklists/portal_form.html', {'portal': 'director', 'store': store, 'form': form, 'title': 'Изменить вопрос'})


def _question_activity(request, question_id, active):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    store = request.current_store
    question = get_object_or_404(
        ChecklistItem,
        pk=question_id,
        section__version__template__store=store,
        section__version__status=ChecklistTemplateVersion.Status.PUBLISHED,
    )
    service = activate_checklist_question if active else deactivate_checklist_question
    service(store, question, request.user, _request_metadata(request))
    messages.success(request, 'Статус вопроса изменён.')
    return redirect('checklists:director_questions')


@store_director_required
def director_question_activate(request, question_id):
    return _question_activity(request, question_id, True)


@store_director_required
def director_question_deactivate(request, question_id):
    return _question_activity(request, question_id, False)


@store_director_required
def director_question_delete(request, question_id):
    store = request.current_store
    question = ChecklistItem.objects.select_related(
        'section__version__template'
    ).filter(
        pk=question_id,
        section__version__template__store=store,
    ).first()
    if question is None:
        already_deleted = AuditLog.objects.filter(
            store=store,
            object_type=ChecklistItem._meta.label_lower,
            object_id=str(question_id),
            action=AuditLog.Action.CHECKLIST_QUESTION_DELETED,
        ).exists()
        if request.method == 'POST' and already_deleted:
            messages.info(request, 'Вопрос уже удалён.')
            return redirect('checklists:director_questions')
        raise Http404
    if request.method == 'POST':
        try:
            result = delete_checklist_question(
                actor=request.user,
                store=store,
                question=question,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            messages.error(request, str(exc))
        else:
            if result['method'] == 'hard_delete':
                messages.success(request, 'Вопрос удалён без возможности восстановления.')
            elif result['method'] == 'removed_from_new_version':
                messages.success(
                    request,
                    'Вопрос исключён из новых чек-листов. История сохранена.',
                )
            else:
                messages.info(request, 'Вопрос уже был исключён из шаблона.')
        return redirect('checklists:director_questions')
    history = get_checklist_question_history(question)
    return render(
        request,
        'checklists/director/question_confirm_delete.html',
        {
            'portal': 'director',
            'store': store,
            'question': question,
            'history': history,
        },
    )


@store_director_required
def director_questions_reorder(request):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    store = request.current_store
    section_code = request.POST.get('section_code')
    ordered_ids = request.POST.getlist('ordered_ids')
    if ordered_ids:
        try:
            normalized_ids = [int(value) for value in ordered_ids]
            reorder_checklist_questions(
                store,
                section_code,
                normalized_ids,
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError, ValueError) as exc:
            return JsonResponse(
                {'ok': False, 'error': str(exc)},
                status=400,
            )
        current_ids = list(
            get_current_questions(store)
            .filter(section__code=section_code)
            .values_list('pk', flat=True)
        )
        return JsonResponse({'ok': True, 'ordered_ids': current_ids})

    question_id = request.POST.get('question_id')
    direction = request.POST.get('direction')
    questions = list(
        get_current_questions(store).filter(section__code=section_code)
    )
    ids = [question.pk for question in questions]
    try:
        index = ids.index(int(question_id))
    except (TypeError, ValueError):
        raise Http404
    swap_index = index - 1 if direction == 'up' else index + 1
    if 0 <= swap_index < len(ids):
        ids[index], ids[swap_index] = ids[swap_index], ids[index]
        reorder_checklist_questions(
            store,
            section_code,
            ids,
            request.user,
            _request_metadata(request),
        )
        messages.success(request, 'Порядок вопросов изменён.')
    return redirect('checklists:director_questions')


@store_director_required
def director_schedule(request):
    store = request.current_store
    schedule, _ = StoreChecklistSchedule.objects.get_or_create(store=store)
    action = request.POST.get('action', 'schedule')
    form = StoreScheduleForm(
        request.POST if request.method == 'POST' and action == 'schedule' else None,
        instance=schedule,
    )
    day_form = StoreDayStatusForm(
        request.POST if request.method == 'POST' and action == 'day_status' else None,
        prefix='day',
    )
    logo_form = StoreLogoForm(
        request.POST if request.method == 'POST' and action == 'logo' else None,
        request.FILES if request.method == 'POST' and action == 'logo' else None,
        instance=store,
        prefix='branding',
    )
    if request.method == 'POST' and action == 'schedule' and form.is_valid():
        try:
            update_store_schedule(
                store,
                form.cleaned_data,
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Расписание сохранено.')
            return redirect('checklists:director_schedule')
    if request.method == 'POST' and action == 'day_status' and day_form.is_valid():
        try:
            set_store_day_status(
                store=store,
                work_date=day_form.cleaned_data['date'],
                status=day_form.cleaned_data['status'],
                comment=day_form.cleaned_data['comment'],
                actor=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(day_form, exc)
        else:
            messages.success(request, 'Статус дня сохранён.')
            return redirect('checklists:director_schedule')
    if request.method == 'POST' and action == 'logo' and logo_form.is_valid():
        try:
            update_store_logo(
                store,
                logo_form.cleaned_data.get('logo'),
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(logo_form, exc)
        else:
            messages.success(request, 'Логотип магазина сохранён.')
            return redirect('checklists:director_schedule')
    selected_month = _parse_month(
        request.GET.get('month'),
        _store_today(store),
    )
    overrides = {
        item.date: item
        for item in StoreDayStatus.objects.filter(
            store=store,
            date__year=selected_month.year,
            date__month=selected_month.month,
        )
    }
    status_labels = dict(ChecklistDayStatus.choices)
    calendar_days = []
    for work_date in iter_month_dates(selected_month):
        status = get_store_day_status(store, work_date)
        calendar_days.append(
            {
                'date': work_date,
                'status': status,
                'status_label': status_labels[status],
                'override': overrides.get(work_date),
            }
        )
    return render(
        request,
        'checklists/director/schedule.html',
        {
            'portal': 'director',
            'store': store,
            'form': form,
            'day_form': day_form,
            'logo_form': logo_form,
            'calendar_days': calendar_days,
            'selected_month': selected_month,
            'previous_month': _adjacent_month(selected_month, -1),
            'next_month': _adjacent_month(selected_month, 1),
        },
    )


@store_director_required
def director_notifications(request):
    if request.method == 'GET':
        return redirect('checklists:telegram_settings')
    store = request.current_store
    settings_obj, _ = StoreNotificationSettings.objects.get_or_create(
        store=store,
        defaults={'is_active': False},
    )
    form = StoreNotificationForm(request.POST or None, instance=settings_obj)
    if request.method == 'POST' and form.is_valid():
        try:
            update_store_notification_settings(store, form.cleaned_data, request.user, _request_metadata(request))
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Настройки уведомлений сохранены.')
            return redirect('checklists:director_notifications')
    return render(
        request,
        'checklists/director/notifications.html',
        {
            'portal': 'director',
            'store': store,
            'form': form,
            'test_form': TelegramTestForm(),
        },
    )


@store_director_required
def director_notification_test(request):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    form = TelegramTestForm(request.POST)
    if form.is_valid():
        try:
            send_store_test_notification(
                request.current_store,
                request.user,
                _request_metadata(request),
            )
        except (ValidationError, TelegramDeliveryError, ChecklistServiceError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Тестовое сообщение отправлено.')
    else:
        messages.error(request, 'Нужно явно подтвердить отправку.')
    return redirect('checklists:director_notifications')


@store_director_required
def director_tasks(request):
    store = request.current_store
    query = StoreAdHocTask.objects.filter(store=store).select_related(
        'created_by',
        'created_by_telegram_binding',
        'completed_by_employee',
        'daily_checklist',
    )
    date_from = _parse_date(request.GET.get('date_from'))
    date_to = _parse_date(request.GET.get('date_to'))
    section = request.GET.get('section', '')
    status = request.GET.get('status', '')
    source = request.GET.get('source', '')
    search = request.GET.get('q', '').strip()
    if date_from:
        query = query.filter(date__gte=date_from)
    if date_to:
        query = query.filter(date__lte=date_to)
    if section in StoreAdHocTask.SectionCode.values:
        query = query.filter(section_code=section)
    if status in StoreAdHocTask.Status.values:
        query = query.filter(status=status)
    if source in StoreAdHocTask.Source.values:
        query = query.filter(source=source)
    if search:
        query = query.filter(
            Q(text__icontains=search) | Q(description__icontains=search)
        )
    if request.GET.get('incomplete') == '1':
        query = query.exclude(
            status__in=(
                StoreAdHocTask.Status.COMPLETED,
                StoreAdHocTask.Status.CANCELLED,
            )
        )
    page = Paginator(query.order_by('-date', '-created_at'), 30).get_page(
        request.GET.get('page')
    )
    return render(
        request,
        'checklists/director/tasks.html',
        {
            'portal': _portal_name(request),
            'store': store,
            'page': page,
            'section_choices': StoreAdHocTask.SectionCode.choices,
            'status_choices': StoreAdHocTask.Status.choices,
            'source_choices': StoreAdHocTask.Source.choices,
            'filters': request.GET,
            'querystring': _querystring_without_page(request),
            'task_admin_scope': False,
        },
    )


@store_director_required
def director_task_create(request):
    store = request.current_store
    form = StoreAdHocTaskForm(
        request.POST or None,
        initial={'date': _store_today(store), 'is_required': True},
    )
    if request.method == 'POST' and form.is_valid():
        try:
            task = create_ad_hoc_task(
                store=store,
                date=form.cleaned_data['date'],
                section_code=form.cleaned_data['section_code'],
                text=form.cleaned_data['text'],
                description=form.cleaned_data['description'],
                is_required=form.cleaned_data['is_required'],
                source=StoreAdHocTask.Source.WEB,
                created_by=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Разовая задача создана.')
            return redirect('checklists:director_task_detail', task_id=task.pk)
    return render(
        request,
        'checklists/director/task_form.html',
        {
            'portal': _portal_name(request),
            'store': store,
            'form': form,
            'title': 'Добавить задачу',
            'cancel_url': reverse('checklists:director_tasks'),
        },
    )


def get_director_task_for_user(request, task_id):
    queryset = StoreAdHocTask.objects.select_related(
        'created_by',
        'created_by_telegram_binding',
        'completed_by_employee',
        'daily_checklist',
    )

    if request.user.is_superuser:
        return get_object_or_404(
            queryset,
            pk=task_id,
        )

    return get_object_or_404(
        queryset,
        pk=task_id,
        store=request.current_store,
    )

@store_director_required
def director_task_detail(request, task_id):
    store = getattr(request, 'current_store', None)

    tasks = StoreAdHocTask.objects.select_related(
        'created_by',
        'created_by_telegram_binding',
        'completed_by_employee',
        'daily_checklist',
    )

    if request.user.is_superuser:
        task = get_object_or_404(tasks, pk=task_id)
    else:
        task = get_object_or_404(
            tasks,
            pk=task_id,
            store=store,
        )
    history = AuditLog.objects.filter(
        object_type=StoreAdHocTask._meta.label_lower,
        object_id=str(task.pk),
    )

    if not request.user.is_superuser:
        history = history.filter(store=store)

    history = history.select_related('actor')
    return render(
        request,
        'checklists/director/task_detail.html',
        {
            'portal': _portal_name(request),
            'store': store,
            'task': task,
            'history': history,
            'can_manage_task': (
                request.user.is_superuser
                or task.created_by_id == request.user.pk
            ),
            'can_delete_task': True,
        },
    )


@store_director_required
def director_task_edit(request, task_id):
    store = request.current_store
    task = get_object_or_404(StoreAdHocTask, pk=task_id, store=store)
    if not request.user.is_superuser and task.created_by_id != request.user.pk:
        return HttpResponseForbidden(
            'Директор может редактировать только созданную им задачу.'
        )
    form = StoreAdHocTaskForm(
        request.POST or None,
        initial=StoreAdHocTaskForm.initial_from_task(task),
    )
    if request.method == 'POST' and form.is_valid():
        try:
            task = update_ad_hoc_task(
                task,
                data=form.cleaned_data,
                actor=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Задача изменена.')
            return redirect('checklists:director_task_detail', task_id=task.pk)
    return render(
        request,
        'checklists/director/task_form.html',
        {
            'portal': _portal_name(request),
            'store': store,
            'form': form,
            'title': 'Изменить задачу',
            'task': task,
            'cancel_url': reverse(
                'checklists:director_task_detail',
                args=[task.pk],
            ),
        },
    )


@store_director_required
def director_task_delete(request, task_id):
    store = getattr(request, 'current_store', None)
    task = get_director_task_for_user(request, task_id)

    if request.method == 'POST':
        try:
            delete_ad_hoc_task(
                task,
                actor=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Задача удалена.')
            return redirect('checklists:director_tasks')
    return render(
        request,
        'checklists/director/task_confirm_delete.html',
        {
            'portal': _portal_name(request),
            'store': store,
            'task': task,
            'cancel_url': reverse(
                'checklists:director_task_detail',
                args=[task.pk],
            ),
        },
    )


@store_director_required
def director_task_cancel(request, task_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Отмена доступна только через POST.')
    store = getattr(request, 'current_store', None)
    task = get_director_task_for_user(request, task_id)

    if not request.user.is_superuser and task.created_by_id != request.user.pk:
        return HttpResponseForbidden(
            'Директор может отменить только созданную им задачу.'
        )

    try:
        cancel_ad_hoc_task(
            task,
            actor=request.user,
            request_metadata=_request_metadata(request),
        )
    except (ChecklistServiceError, ValidationError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Задача отменена.')
    return redirect('checklists:director_task_detail', task_id=task.pk)


@store_director_required
def director_reports(request):
    store = request.current_store
    period = _report_period_v2(request, store)
    dashboard = build_report_dashboard(store, period)
    daily_rows = build_daily_rows(store, period)
    return render(
        request,
        'checklists/director/reports_index.html',
        {
            'portal': 'director',
            'store': store,
            'period': period,
            'generated_at': timezone.now(),
            'daily_rows': daily_rows,
            **dashboard,
        },
    )


PRICE_TAG_SELLER_TIPS = (
    (
        'Отличная работа — ценники под контролем!',
        'Сначала уточните задачу покупателя, а затем показывайте товар: '
        'рекомендация будет восприниматься как помощь, а не как давление.',
    ),
    (
        'Так держать — покупателю всё будет понятно!',
        'Не перечисляйте все характеристики подряд. Выберите одну главную '
        'выгоду, которая решает задачу именно этого покупателя.',
    ),
    (
        'Профессиональный подход: проверено и готово к печати!',
        'Если покупатель сравнивает варианты, назовите простое различие: '
        'кому и в какой ситуации лучше подходит каждый из них.',
    ),
    (
        'Ценник готов — ещё один повод собой гордиться!',
        'Покажите товар в использовании: короткий живой пример запоминается '
        'лучше, чем длинный список технических параметров.',
    ),
    (
        'Отличная подготовка — продажи любят порядок!',
        'Спросите, что для покупателя важнее: цена, удобство, надёжность или '
        'внешний вид. Ответ сразу подскажет правильный аргумент.',
    ),
    (
        'Вы всё проверили — покупатель это оценит!',
        'Перед завершением разговора повторите выбранную модель и её главную '
        'пользу. Так покупатель увереннее принимает решение.',
    ),
    (
        'Красиво, понятно, вовремя — отличная работа!',
        'Дополнительный товар предлагайте через пользу: не «возьмите ещё», '
        'а «это поможет установить, защитить или удобнее использовать покупку».',
    ),
    (
        'Ценник не забыт — уровень ответственного продавца!',
        'Если вопрос покупателя требует проверки, честно скажите об этом и '
        'уточните информацию. Точный ответ укрепляет доверие лучше догадки.',
    ),
    (
        'Ещё один ценник готов — стабильность побеждает хаос!',
        'После демонстрации задайте простой вопрос: «Как вам этот вариант?» '
        'Ответ покажет, какой аргумент нужен дальше.',
    ),
    (
        'Отличный темп — магазин выглядит профессионально!',
        'Цена звучит убедительнее вместе с результатом: объясните, что '
        'покупатель получит за эти деньги и какую проблему товар решит.',
    ),
)


def _ensure_price_tag_profiles(store):
    profiles = list(store.price_tag_templates.all())
    if profiles:
        return profiles
    StorePriceTagTemplate.objects.bulk_create([
        StorePriceTagTemplate(
            store=store,
            name='ES-AUTO',
            site_domain='es-auto.ru',
        ),
        StorePriceTagTemplate(
            store=store,
            name='PINEL',
            site_domain='pinel.ru',
            layout_template=StorePriceTagTemplate.LayoutTemplate.PINEL,
        ),
    ])
    return list(store.price_tag_templates.all())


def _price_tag_profile_for_url(url, profiles):
    matches = [
        profile for profile in profiles
        if site_url_matches(url, profile.site_domain)
    ]
    return max(matches, key=lambda item: len(item.site_domain), default=None)


def _price_tag_property_limit(profile):
    return min(profile.max_properties, 5)


def _ensure_product_price_tag_category(profile, product, property_limit):
    if product.category_rule:
        return product
    category_name = (
        product.product_type or product.category_name or product.secondary_name
        or 'Товары'
    )
    category_name = category_name.strip()[:120] or 'Товары'
    property_names = []
    for name, _ in product.properties:
        if name not in property_names:
            property_names.append(name)
    category, created = StorePriceTagCategory.objects.get_or_create(
        profile=profile,
        name=category_name,
        defaults={
            'available_property_names': property_names,
            'property_names': '\n'.join(property_names[:property_limit]),
        },
    )
    if not created:
        discovered = [
            *category.available_property_names,
            *[
                name for name in property_names
                if name not in category.available_property_names
            ],
        ]
        if discovered != category.available_property_names:
            category.available_property_names = discovered
            category.save(update_fields=(
                'available_property_names', 'updated_at',
            ))
    product.category_rule = category
    selected_names = category.property_name_list or property_names[:property_limit]
    select_product_properties(product, selected_names, property_limit)
    return product


def _ordered_category_property_names(category):
    selected = [
        name for name in category.property_name_list
        if name in category.available_property_names
    ]
    return [
        *selected,
        *[
            name for name in category.available_property_names
            if name not in selected
        ],
    ]


def _next_price_tag_seller_tip(store):
    last_tip = (
        PriceTagGeneration.objects.filter(store=store)
        .exclude(sales_tip='')
        .values_list('sales_tip', flat=True)
        .first()
    )
    if not last_tip:
        return PRICE_TAG_SELLER_TIPS[store.pk % len(PRICE_TAG_SELLER_TIPS)]
    last_index = next(
        (
            index for index, (_, tip) in enumerate(PRICE_TAG_SELLER_TIPS)
            if tip == last_tip
        ),
        -1,
    )
    return PRICE_TAG_SELLER_TIPS[(last_index + 1) % len(PRICE_TAG_SELLER_TIPS)]


def _record_price_tag_generation(store, user, products):
    if not products:
        return None
    with transaction.atomic():
        seller_praise, sales_tip = _next_price_tag_seller_tip(store)
        generation = PriceTagGeneration.objects.create(
            store=store,
            created_by=user,
            item_count=len(products),
            seller_praise=seller_praise,
            sales_tip=sales_tip,
        )
        PriceTagGenerationItem.objects.bulk_create([
            PriceTagGenerationItem(
                generation=generation,
                source_url=product.url,
                product_name=product.prominent_name[:500],
                profile_name=product.price_tag_profile.name,
                category_name=(
                    product.category_rule.name if product.category_rule else ''
                ),
                sort_order=index,
            )
            for index, product in enumerate(products)
        ])
        keep_ids = list(
            PriceTagGeneration.objects.filter(store=store)
            .values_list('pk', flat=True)[:20]
        )
        PriceTagGeneration.objects.filter(store=store).exclude(
            pk__in=keep_ids,
        ).delete()
    return generation


@price_tag_tool_required
def price_tags(request):
    store = request.current_store
    can_edit_template = (
        is_system_admin(request.user) or is_store_director(request.user)
    )
    profiles = _ensure_price_tag_profiles(store)
    active_profiles = [profile for profile in profiles if profile.is_active] or profiles
    links_form = PriceTagLinksForm()
    products = []
    import_errors = []
    category_panels = []
    unmatched_products = []
    generation = None
    action = request.POST.get('action') if request.method == 'POST' else ''
    force_refresh = (
        can_edit_template and request.POST.get('force_refresh') == '1'
    )

    if action == 'generate':
        links_form = PriceTagLinksForm(request.POST)
        if links_form.is_valid():
            for url in links_form.cleaned_data['urls']:
                profile = _price_tag_profile_for_url(url, active_profiles)
                if not profile:
                    import_errors.append({
                        'url': url,
                        'message': (
                            'Для домена этой ссылки не настроен профиль '
                            'интернет-магазина.'
                        ),
                    })
                    continue
                try:
                    property_limit = _price_tag_property_limit(profile)
                    imported_product = (
                        import_product(url, force_refresh=True)
                        if force_refresh else import_product(url)
                    )
                    product = apply_category_rules(
                        imported_product,
                        list(profile.categories.filter(is_active=True)),
                        property_limit,
                    )
                    product = _ensure_product_price_tag_category(
                        profile, product, property_limit,
                    )
                    product.price_tag_profile = profile
                    corrections = profile.name_corrections.filter(
                        category=product.category_rule,
                    )[:20]
                    suggest_product_name(product, corrections)
                    product.tracking_url = build_qr_url(
                        product.url,
                        profile.qr_utm_parameters,
                    )
                    products.append(product)
                except ProductImportError as exc:
                    import_errors.append({'url': url, 'message': str(exc)})

            panels = {}
            for product in products:
                category = product.category_rule
                if not category:
                    unmatched_products.append(product)
                    continue
                panel = panels.setdefault(category.pk, {
                    'category': category,
                    'profile': product.price_tag_profile,
                    'available_names': list(category.available_property_names),
                    'products': [],
                })
                panel['products'].append(product)
                for name, _ in product.properties:
                    if name not in panel['available_names']:
                        panel['available_names'].append(name)
            for panel in panels.values():
                category = panel['category']
                if panel['available_names'] != category.available_property_names:
                    category.available_property_names = panel['available_names']
                    category.save(update_fields=(
                        'available_property_names', 'updated_at',
                    ))
                selected = [
                    name for name in category.property_name_list
                    if name in panel['available_names']
                ]
                panel['available_names'] = [
                    *selected,
                    *[
                        name for name in panel['available_names']
                        if name not in selected
                    ],
                ]
                property_limit = _price_tag_property_limit(panel['profile'])
                if not selected:
                    selected = panel['available_names'][:property_limit]
                panel['selected_names'] = selected
                panel['max_properties'] = property_limit
                for product in panel.pop('products'):
                    select_product_properties(
                        product,
                        selected,
                        property_limit,
                    )
                category_panels.append(panel)
            generation = _record_price_tag_generation(
                store,
                request.user,
                products,
            )

    generation_history = list(
        PriceTagGeneration.objects.filter(store=store)
        .select_related('created_by')
        .prefetch_related('items')[:20]
    )

    return render(
        request,
        'checklists/director/price_tags.html',
        {
            'portal': (
                'director'
                if is_system_admin(request.user) or is_store_director(request.user)
                else 'price_tags'
            ),
            'store': store,
            'price_tag_profiles': active_profiles,
            'links_form': links_form,
            'category_panels': category_panels,
            'unmatched_products': unmatched_products,
            'products': products,
            'product_sheets': [
                products[index:index + 4]
                for index in range(0, len(products), 4)
            ],
            'import_errors': import_errors,
            'printed_on': _store_today(store),
            'can_edit_template': can_edit_template,
            'generation_history': generation_history,
            'seller_praise': generation.seller_praise if generation else '',
            'sales_tip': generation.sales_tip if generation else '',
        },
    )


@price_tag_tool_required
def price_tag_qr(request):
    value = request.GET.get('data', '').strip()
    parts = parse.urlsplit(value)
    if (
        not value
        or len(value) > 2000
        or parts.scheme not in {'http', 'https'}
        or not parts.netloc
    ):
        return HttpResponse('Некорректная ссылка QR-кода.', status=400)
    response = HttpResponse(render_qr_png(value), content_type='image/png')
    response['Cache-Control'] = 'private, max-age=3600'
    return response


@price_tag_tool_required
def price_tag_image(request):
    value = request.GET.get('url', '').strip()
    if not value or len(value) > 2000:
        return HttpResponse('Некорректная ссылка фото.', status=400)
    try:
        content, content_type = download_product_image(value)
    except ProductImportError as exc:
        return HttpResponse(str(exc), status=400)
    response = HttpResponse(content, content_type=content_type)
    response['Cache-Control'] = 'private, max-age=3600'
    return response


@require_POST
@price_tag_tool_required
def price_tag_name_correction(request):
    profile = get_object_or_404(
        StorePriceTagTemplate,
        pk=request.POST.get('profile_id'),
        store=request.current_store,
    )
    category = None
    if request.POST.get('category_id'):
        category = get_object_or_404(
            StorePriceTagCategory,
            pk=request.POST.get('category_id'),
            profile=profile,
        )
    source_url = request.POST.get('source_url', '').strip()
    original_name = request.POST.get('original_name', '').strip()
    corrected_name = request.POST.get('corrected_name', '').strip()
    if (
        not site_url_matches(source_url, profile.site_domain)
        or len(source_url) > 500
        or not original_name
        or not corrected_name
        or len(original_name) > 500
        or len(corrected_name) > 500
    ):
        return JsonResponse({
            'ok': False,
            'message': 'Не удалось сохранить название.',
        }, status=400)
    correction, _ = PriceTagNameCorrection.objects.update_or_create(
        profile=profile,
        source_url=source_url,
        defaults={
            'category': category,
            'original_name': original_name,
            'corrected_name': corrected_name,
            'created_by': request.user,
        },
    )
    return JsonResponse({'ok': True, 'name': correction.corrected_name})


@require_POST
@price_tag_tool_required
def price_tag_category_properties(request):
    category = get_object_or_404(
        StorePriceTagCategory,
        pk=request.POST.get('category_id'),
        profile__store=request.current_store,
    )
    names = list(dict.fromkeys(
        name.strip() for name in request.POST.getlist('property_names')
        if name.strip()
    ))
    property_limit = _price_tag_property_limit(category.profile)
    available_names = set(category.available_property_names)
    if len(names) > property_limit or any(
        len(name) > 160 for name in names
    ) or (available_names and any(name not in available_names for name in names)):
        return JsonResponse({
            'ok': False,
            'message': (
                f'Можно выбрать не более '
                f'{property_limit} свойств.'
            ),
        }, status=400)
    category.property_names = '\n'.join(names)
    category.save(update_fields=('property_names', 'updated_at'))
    return JsonResponse({'ok': True, 'selected': names})


@require_POST
@price_tag_tool_required
def price_tag_category_promotion(request):
    category = get_object_or_404(
        StorePriceTagCategory,
        pk=request.POST.get('category_id'),
        profile__store=request.current_store,
    )
    title = request.POST.get('promotion_title', '').strip()
    details = request.POST.get('promotion_details', '').strip()
    if len(title) > 100 or len(details) > 200:
        return JsonResponse({
            'ok': False,
            'message': 'Текст акции слишком длинный.',
        }, status=400)
    category.promotion_title = title
    category.promotion_details = details
    category.save(update_fields=(
        'promotion_title', 'promotion_details', 'updated_at',
    ))
    return JsonResponse({
        'ok': True,
        'promotion_title': title,
        'promotion_details': details,
    })


@price_tag_tool_required
def price_tag_profile(request):
    if not (is_system_admin(request.user) or is_store_director(request.user)):
        return HttpResponseForbidden(
            'Профиль магазина может менять администратор или директор.'
        )
    store = request.current_store
    profiles = _ensure_price_tag_profiles(store)
    profile_id = request.POST.get('profile_id') or request.GET.get('profile')
    is_new = (
        request.GET.get('new') == '1'
        or request.POST.get('create_profile') == '1'
    )
    template = None if is_new else next(
        (
            profile for profile in profiles
            if str(profile.pk) == str(profile_id)
        ),
        profiles[0],
    )
    action = request.POST.get('action', '')
    if action == 'save_category' and template:
        category_id = request.POST.get('category_id')
        category = (
            get_object_or_404(
                StorePriceTagCategory,
                pk=category_id,
                profile=template,
            )
            if category_id else StorePriceTagCategory(profile=template)
        )
        category_form = StorePriceTagCategoryForm(
            request.POST,
            instance=category,
            profile=template,
        )
        if category_form.is_valid():
            category_form.save()
            messages.success(request, 'Категория сайта сохранена.')
            return redirect(
                f"{reverse('checklists:price_tag_profile')}?profile={template.pk}"
            )
    elif action == 'delete_category' and template:
        category = get_object_or_404(
            StorePriceTagCategory,
            pk=request.POST.get('category_id'),
            profile=template,
        )
        category.delete()
        messages.success(request, 'Категория сайта удалена.')
        return redirect(
            f"{reverse('checklists:price_tag_profile')}?profile={template.pk}"
        )
    else:
        category_form = StorePriceTagCategoryForm(profile=template)

    instance = template or StorePriceTagTemplate(store=store)
    form = StorePriceTagTemplateForm(
        request.POST if request.method == 'POST' and action == 'save_profile' else None,
        request.FILES if request.method == 'POST' and action == 'save_profile' else None,
        instance=instance,
    )
    if request.method == 'POST' and action == 'save_profile' and form.is_valid():
        template = form.save(commit=False)
        template.store = store
        template.save()
        messages.success(request, 'Профиль интернет-магазина сохранён.')
        return redirect(
            f"{reverse('checklists:price_tag_profile')}?profile={template.pk}"
        )
    return render(request, 'checklists/director/price_tag_profile.html', {
        'portal': 'director',
        'store': store,
        'price_tag_profiles': profiles,
        'price_tag_template': template,
        'form': form,
        'is_new_profile': is_new,
        'category_form': category_form,
        'category_forms': [
            (
                category,
                StorePriceTagCategoryForm(
                    instance=category,
                    profile=template,
                ),
                _ordered_category_property_names(category),
            )
            for category in template.categories.all()
        ] if template else [],
    })


def _report_range(request, store):
    today = _store_today(store)
    start = _parse_date(request.GET.get('start'), today - timedelta(days=30))
    end = _parse_date(request.GET.get('end'), today)
    if start > end:
        start, end = end, start
    return start, end


def _report_period_v2(request, store):
    today = _store_today(store)
    preset = request.GET.get('period', '')
    if preset == 'today':
        start = end = today
    elif preset == 'yesterday':
        start = end = today - timedelta(days=1)
    elif preset == '7days':
        start, end = today - timedelta(days=6), today
    elif preset == '30days':
        start, end = today - timedelta(days=29), today
    elif preset == 'month':
        start, end = today.replace(day=1), today
    else:
        start = _parse_date(
            request.GET.get('date_from') or request.GET.get('start'),
            today - timedelta(days=29),
        )
        end = _parse_date(
            request.GET.get('date_to') or request.GET.get('end'),
            today,
        )
    return make_report_period(start, end)


def _report_common_context(request, store, period):
    return {
        'portal': 'director',
        'store': store,
        'period': period,
        'start': period.date_from,
        'end': period.date_to,
        'generated_at': timezone.now(),
        'querystring': _querystring_without_page(request),
    }


def _csv_response(filename, headers, rows):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('\ufeff')
    writer = csv.writer(response, delimiter=';')
    writer.writerow(headers)
    writer.writerows(rows)
    return response


def _daily_report_matches(row, filters):
    stages = row['stages']
    section = filters.get('section')
    if section:
        stages = [item for item in stages if item['stage'].section_code == section]
        if not stages:
            return False
    stage_status = filters.get('stage_status')
    if stage_status and not any(
        item['stage'].status == stage_status for item in stages
    ):
        return False
    outcome = filters.get('outcome')
    if outcome == 'on_time' and not any(
        item['stage'].status == DailyChecklistStage.Status.COMPLETED
        for item in stages
    ):
        return False
    if outcome == 'late' and not any(
        item['stage'].status == DailyChecklistStage.Status.COMPLETED_LATE
        for item in stages
    ):
        return False
    if outcome == 'not_completed' and not any(
        item['stage'].status
        not in {
            DailyChecklistStage.Status.COMPLETED,
            DailyChecklistStage.Status.COMPLETED_LATE,
        }
        for item in stages
    ):
        return False
    employee = filters.get('employee')
    if employee:
        involved_ids = {item.pk for item in row['participants']}
        involved_ids.update(
            assignment.employee_id for assignment in row['assigned']
        )
        involved_ids.update(
            item['stage'].completed_by_employee_id
            for item in stages
            if item['stage'].completed_by_employee_id
        )
        if employee.pk not in involved_ids:
            return False
    if filters.get('has_failed') and row['failed'] == 0:
        return False
    if filters.get('has_changes') and row['revisions'] == 0:
        return False
    if filters.get('assigned') and not row['assigned']:
        return False
    if filters.get('missing') and not row['missing']['employees_without_actions']:
        return False
    return True


@store_director_required
def director_report_daily(request):
    store = request.current_store
    period = _report_period_v2(request, store)
    start, end = period.date_from, period.date_to
    employee = None
    employee_id = request.GET.get('employee')
    if employee_id:
        employee = get_object_or_404(StoreEmployee, pk=employee_id, store=store)
    filters = {
        'employee': employee,
        'section': request.GET.get('section', ''),
        'stage_status': request.GET.get('stage_status', ''),
        'outcome': request.GET.get('outcome', ''),
        'has_failed': request.GET.get('has_failed') == '1',
        'has_changes': request.GET.get('has_changes') == '1',
        'assigned': request.GET.get('assigned') == '1',
        'missing': request.GET.get('missing') == '1',
    }
    rows = build_daily_rows(store, period)
    if employee:
        rows = [
            row for row in rows
            if employee in row['participants']
            or any(
                assignment.employee_id == employee.pk
                for assignment in row['assignments']
            )
        ]
    if filters['section']:
        for row in rows:
            row['stages'] = [
                item for item in row['stages']
                if item['stage'].section_code == filters['section']
            ]
        rows = [row for row in rows if row['stages']]
    if filters['has_failed']:
        rows = [
            row for row in rows
            if any(stage['failed'] for stage in row['stages'])
        ]
    if filters['has_changes']:
        rows = [
            row for row in rows
            if any(
                answer['revision_count']
                for stage in row['stages']
                for answer in stage['answers']
            )
        ]
    if request.GET.get('format') == 'csv':
        return _csv_response(
            'daily-report.csv',
            (
                'Дата',
                'Этап',
                'Статус',
                'Обязательных',
                'Выполнено',
                'Не выполнено',
                'Без ответа',
            ),
            (
                (
                    row['daily'].checklist_date,
                    stage['stage'].get_section_code_display(),
                    stage['stage'].get_status_display(),
                    stage['required'],
                    stage['completed'],
                    stage['failed'],
                    stage['missing'],
                )
                for row in rows for stage in row['stages']
            ),
        )
    page = Paginator(rows, 25).get_page(request.GET.get('page'))
    return render(
        request,
        'checklists/director/report_daily.html',
        {
            **_report_common_context(request, store, period),
            'rows': page.object_list,
            'page': page,
            'employees': StoreEmployee.objects.filter(store=store),
            'filters': filters,
            'section_choices': DailyChecklistStage.SectionCode.choices,
            'stage_status_choices': DailyChecklistStage.Status.choices,
            'querystring': _querystring_without_page(request),
        },
    )


@store_director_required
def director_report_employees(request):
    store = request.current_store
    period = _report_period_v2(request, store)
    rows = build_employee_rows(store, period)
    employee_id = request.GET.get('employee')
    if employee_id:
        employee = get_object_or_404(StoreEmployee, pk=employee_id, store=store)
        rows = [row for row in rows if row['employee'].pk == employee.pk]
    activity = request.GET.get('activity', '')
    if activity in {'active', 'inactive'}:
        rows = [
            row for row in rows
            if row['employee'].is_active == (activity == 'active')
        ]
    if request.GET.get('only_problems') == '1':
        rows = [row for row in rows if row['problem_count']]
    ordering = request.GET.get('ordering', '-problems')
    sorters = {
        '-problems': lambda row: (-row['problem_count'], row['employee'].display_name),
        'participation': lambda row: (row['participation_rate'], row['employee'].display_name),
        '-failed': lambda row: (-row['failed'], row['employee'].display_name),
        '-overdue': lambda row: (-row['overdue_tasks'], row['employee'].display_name),
        '-revisions': lambda row: (-row['revisions'], row['employee'].display_name),
        'name': lambda row: row['employee'].display_name,
    }
    rows.sort(key=sorters.get(ordering, sorters['-problems']))
    if request.GET.get('format') == 'csv':
        return _csv_response(
            'employees-report.csv',
            (
                'Сотрудник', 'Смены', 'Участие, %', 'Ответы',
                'Не выполнено', 'Пропуски', 'Задачи выполнены',
                'Задачи просрочены', 'Изменения', 'Статус',
            ),
            (
                (
                    row['employee'].display_name,
                    row['shift_count'],
                    row['participation_rate'],
                    row['answers_completed'],
                    row['failed'],
                    row['missing_required'],
                    row['tasks_completed'],
                    row['overdue_tasks'],
                    row['revisions'],
                    row['health_label'],
                ) for row in rows
            ),
        )
    page = Paginator(rows, 25).get_page(request.GET.get('page'))
    return render(
        request,
        'checklists/director/report_employees.html',
        {
            **_report_common_context(request, store, period),
            'rows': page.object_list,
            'page': page,
            'employee_id': employee_id,
            'employees': StoreEmployee.objects.filter(store=store),
            'activity': activity,
            'ordering': ordering,
            'only_problems': request.GET.get('only_problems') == '1',
            'querystring': _querystring_without_page(request),
        },
    )


@store_director_required
def director_report_revisions(request):
    store = request.current_store
    period = _report_period_v2(request, store)
    employee = None
    employee_id = request.GET.get('employee')
    if employee_id:
        employee = get_object_or_404(StoreEmployee, pk=employee_id, store=store)
    analytics = get_revision_analytics(
        store,
        period,
        {
            'employee': employee,
            'stage': request.GET.get('stage') or request.GET.get('section'),
            'only_after_deadline': request.GET.get('only_after_deadline') == '1',
        },
    )
    rows = analytics['rows']
    if request.GET.get('format') == 'csv':
        return _csv_response(
            'revisions-report.csv',
            (
                'Дата', 'Этап', 'Вопрос', 'Сотрудник', 'Старое значение',
                'Новое значение', 'Причина', 'После дедлайна', 'Actor',
            ),
            (
                (
                    row.changed_at.isoformat(),
                    row.answer.daily_item.section_name,
                    row.answer.daily_item.item_text,
                    row.changed_by_employee.display_name if row.changed_by_employee else '',
                    row.previous_integer_value if row.previous_integer_value is not None else row.previous_status,
                    row.new_integer_value if row.new_integer_value is not None else row.new_status,
                    row.change_reason,
                    'Да' if row.after_deadline else 'Нет',
                    row.changed_by_user.username if row.changed_by_user else '',
                ) for row in rows
            ),
        )
    page = Paginator(rows, 50).get_page(request.GET.get('page'))
    return render(
        request,
        'checklists/director/report_revisions.html',
        {
            **_report_common_context(request, store, period),
            'rows': page.object_list,
            'page': page,
            'analytics': analytics,
            'employee': employee,
            'section': request.GET.get('stage') or request.GET.get('section', ''),
            'employees': StoreEmployee.objects.filter(store=store),
            'section_choices': DailyChecklistStage.SectionCode.choices,
            'querystring': _querystring_without_page(request),
        },
    )


@store_director_required
def director_report_employee_detail(request, employee_id):
    store = request.current_store
    employee = get_object_or_404(StoreEmployee, pk=employee_id, store=store)
    period = _report_period_v2(request, store)
    row = next(
        item for item in build_employee_rows(store, period)
        if item['employee'].pk == employee.pk
    )
    return render(
        request,
        'checklists/director/report_employee_detail.html',
        {
            **_report_common_context(request, store, period),
            'employee': employee,
            'row': row,
        },
    )


@store_director_required
def director_report_recurring(request):
    store = request.current_store
    period = _report_period_v2(request, store)
    data = build_recurring_problems(store, period)
    if request.GET.get('format') == 'csv':
        return _csv_response(
            'problems-report.csv',
            ('Категория', 'Проблема', 'Повторов', 'Последняя дата'),
            (
                (
                    row['category'],
                    row['problem'],
                    row['count'],
                    row.get('last_date', ''),
                ) for row in data['rows']
            ),
        )
    return render(
        request,
        'checklists/director/report_recurring.html',
        {**_report_common_context(request, store, period), **data},
    )


@store_director_required
def director_report_tasks(request):
    store = request.current_store
    period = _report_period_v2(request, store)
    filters = {
        'status': request.GET.get('status', ''),
        'source': request.GET.get('source', ''),
        'stage': request.GET.get('stage', ''),
    }
    data = get_task_analytics(store, period, filters)
    if request.GET.get('format') == 'csv':
        return _csv_response(
            'tasks-report.csv',
            (
                'Дата', 'Этап', 'Задача', 'Статус', 'Источник',
                'Создатель', 'Исполнитель', 'Комментарий',
            ),
            (
                (
                    task.date,
                    task.get_section_code_display(),
                    task.text,
                    task.get_status_display(),
                    task.get_source_display(),
                    task.created_by.username if task.created_by else 'Telegram',
                    task.completed_by_employee.display_name if task.completed_by_employee else '',
                    task.completion_comment,
                ) for task in data['rows']
            ),
        )
    return render(
        request,
        'checklists/director/report_tasks.html',
        {
            **_report_common_context(request, store, period),
            **data,
            'filters': filters,
            'status_choices': StoreAdHocTask.Status.choices,
            'source_choices': StoreAdHocTask.Source.choices,
            'section_choices': StoreAdHocTask.SectionCode.choices,
        },
    )


@store_director_required
def director_report_problems(request):
    store = request.current_store
    period = _report_period_v2(request, store)
    facts = collect_store_facts(store, period)
    rows = identify_problems(store, period, facts)
    problem_type = request.GET.get('problem_type', '')
    if problem_type:
        rows = [row for row in rows if row['type'] == problem_type]
    if request.GET.get('format') == 'csv':
        return _csv_response(
            'attention-report.csv',
            ('Дата', 'Критичность', 'Проблема', 'Подробность'),
            (
                (
                    row['date'],
                    row['severity'],
                    row['title'],
                    row['detail'],
                ) for row in rows
            ),
        )
    return render(
        request,
        'checklists/director/report_problems.html',
        {
            **_report_common_context(request, store, period),
            'rows': rows,
            'problem_type': problem_type,
        },
    )


@system_admin_required
def system_reports(request):
    today = timezone.localdate()
    period = make_report_period(today, today)
    rows = []
    for store in Store.objects.filter(is_active=True):
        dashboard = build_report_dashboard(store, period)
        rows.append({
            'store': store,
            'health': dashboard['health'],
            'overdue_tasks': next(
                card['value'] for card in dashboard['cards']
                if card['code'] == 'overdue_tasks'
            ),
            'telegram_errors': len(dashboard['facts']['outbound_errors']),
            'has_daily': bool(dashboard['facts']['dailies']),
        })
    rows.sort(
        key=lambda row: (
            {'critical': 0, 'attention': 1, 'normal': 2}[row['health']['code']],
            row['store'].name,
        )
    )
    return render(
        request,
        'checklists/system_admin/reports.html',
        {
            'portal': 'system_admin',
            'period': period,
            'generated_at': timezone.now(),
            'rows': rows,
        },
    )


@store_director_required
def director_checklist_detail(request, checklist_id):
    store = request.current_store
    daily = get_object_or_404(
        DailyChecklist.objects.select_related(
            'terminal_account__user',
            'employee__user',
            'reopened_by',
        ).prefetch_related(
            'stages__completed_by_employee',
            'items__answer__answered_by_employee',
            'items__answer__last_edited_by_employee',
            'items__answer__revisions__changed_by_employee',
            'stages__notifications',
        ),
        pk=checklist_id,
        store=store,
    )
    stages = list(daily.stages.all())
    items = list(daily.items.all())
    for stage in stages:
        stage.report_lateness = (
            stage.completed_at - stage.deadline_at
            if stage.completed_at and stage.completed_at > stage.deadline_at
            else None
        )
    relevant_objects = (
        Q(object_type=DailyChecklist._meta.label_lower, object_id=str(daily.pk))
        | Q(
            object_type=DailyChecklistStage._meta.label_lower,
            object_id__in=[str(stage.pk) for stage in stages],
        )
        | Q(
            object_type=ChecklistAnswer._meta.label_lower,
            object_id__in=[str(item.answer.pk) for item in items],
        )
    )
    safe_audit = AuditLog.objects.filter(store=store).filter(
        relevant_objects
    ).exclude(
        action__in=(
            AuditLog.Action.USER_PASSWORD_RESET,
        )
    ).order_by('-created_at')[:50]
    return render(
        request,
        'checklists/director/checklist_detail.html',
        {
            'portal': 'director',
            'store': store,
            'daily': daily,
            'stages': stages,
            'items': items,
            'safe_audit': safe_audit,
            'reopen_form': ReopenStageForm(),
        },
    )


@store_director_required
def director_stage_reopen(request, checklist_id, section_code):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    store = request.current_store
    daily = get_object_or_404(DailyChecklist, pk=checklist_id, store=store)
    form = ReopenStageForm(request.POST)
    if form.is_valid():
        try:
            reopen_stage_with_reason(
                daily,
                section_code,
                form.cleaned_data['reason'],
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Этап повторно открыт.')
    else:
        messages.error(request, 'Укажите причину минимум из 5 символов.')
    return redirect('checklists:director_checklist_detail', checklist_id=daily.pk)


@system_admin_required
def system_admin_dashboard(request):
    today = timezone.localdate()
    stores = Store.objects.annotate(
        active_director_count=Count(
            'employees',
            filter=Q(
                employees__role=EmployeeProfile.Role.STORE_DIRECTOR,
                employees__is_active=True,
                employees__user__is_active=True,
            ),
        ),
        active_account_count=Count(
            'employees',
            filter=Q(
                employees__role=EmployeeProfile.Role.STORE_ACCOUNT,
                employees__is_active=True,
                employees__user__is_active=True,
            ),
        ),
    )
    profiles = EmployeeProfile.objects.filter(is_active=True, user__is_active=True)
    managed_store = resolve_managed_store(request)
    data = {
        'stores_total': stores.count(),
        'stores_active': stores.filter(is_active=True).count(),
        'stores_inactive': stores.filter(is_active=False).count(),
        'store_accounts': profiles.filter(role=EmployeeProfile.Role.STORE_ACCOUNT).count(),
        'directors': profiles.filter(role=EmployeeProfile.Role.STORE_DIRECTOR).count(),
        'system_admins': profiles.filter(role=EmployeeProfile.Role.SYSTEM_ADMIN).count(),
        'employees': StoreEmployee.objects.count(),
        'daily_today': DailyChecklist.objects.filter(checklist_date=today).count(),
        'completed_stages': DailyChecklistStage.objects.filter(status__in=(DailyChecklistStage.Status.COMPLETED, DailyChecklistStage.Status.COMPLETED_LATE)).count(),
        'overdue_stages': DailyChecklistStage.objects.filter(status=DailyChecklistStage.Status.OVERDUE).count(),
        'telegram_errors': ChecklistNotification.objects.filter(status=ChecklistNotification.Status.FAILED).count(),
        'recent_actions': AuditLog.objects.select_related('actor', 'store')[:10],
        'recent_stores': stores.order_by('-created_at')[:10],
        'managed_store_choices': stores.filter(is_active=True).order_by('name'),
        'stores_without_director': stores.filter(active_director_count=0),
        'stores_without_account': stores.filter(active_account_count=0),
        'stores_without_schedule': stores.filter(checklist_schedule__isnull=True),
        'stores_without_notifications': stores.filter(notification_settings__isnull=True),
        'managed_store': managed_store,
    }
    if managed_store:
        store_today = _store_today(managed_store)
        data['managed_summary'] = {
            'current_stages': DailyChecklistStage.objects.filter(
                daily_checklist__store=managed_store,
                daily_checklist__checklist_date=store_today,
            ).count(),
            'incomplete_questions': ChecklistAnswer.objects.filter(
                daily_item__daily_checklist__store=managed_store,
                daily_item__daily_checklist__checklist_date=store_today,
                status=ChecklistAnswer.Status.PENDING,
            ).count(),
            'active_tasks': StoreAdHocTask.objects.filter(
                store=managed_store,
                status__in=(
                    StoreAdHocTask.Status.PLANNED,
                    StoreAdHocTask.Status.ACTIVE,
                ),
            ).count(),
            'overdue_tasks': StoreAdHocTask.objects.filter(
                store=managed_store,
                date__lt=store_today,
                status__in=(
                    StoreAdHocTask.Status.PLANNED,
                    StoreAdHocTask.Status.ACTIVE,
                ),
            ).count(),
            'employees_on_shift': DailyShiftAssignment.objects.filter(
                store=managed_store,
                work_date=store_today,
            ).count(),
            'telegram_errors': TelegramOutboundMessage.objects.filter(
                store=managed_store,
                status=TelegramOutboundMessage.Status.FAILED,
            ).count(),
            'telegram_pending': TelegramOutboundMessage.objects.filter(
                store=managed_store,
                status=TelegramOutboundMessage.Status.PENDING,
            ).count(),
        }
    return render(request, 'checklists/system_admin/dashboard.html', {'portal': 'system_admin', **data})


@system_admin_required
def system_tasks(request):
    query = StoreAdHocTask.objects.select_related(
        'store',
        'created_by',
        'created_by_telegram_binding',
        'completed_by_employee',
    )
    date_from = _parse_date(request.GET.get('date_from'))
    date_to = _parse_date(request.GET.get('date_to'))
    section = request.GET.get('section', '')
    status = request.GET.get('status', '')
    source = request.GET.get('source', '')
    search = request.GET.get('q', '').strip()
    if date_from:
        query = query.filter(date__gte=date_from)
    if date_to:
        query = query.filter(date__lte=date_to)
    if section in StoreAdHocTask.SectionCode.values:
        query = query.filter(section_code=section)
    if status in StoreAdHocTask.Status.values:
        query = query.filter(status=status)
    if source in StoreAdHocTask.Source.values:
        query = query.filter(source=source)
    if search:
        query = query.filter(
            Q(text__icontains=search)
            | Q(description__icontains=search)
            | Q(store__name__icontains=search)
        )
    if request.GET.get('incomplete') == '1':
        query = query.exclude(
            status__in=(
                StoreAdHocTask.Status.COMPLETED,
                StoreAdHocTask.Status.CANCELLED,
            )
        )
    page = Paginator(query.order_by('-date', '-created_at'), 50).get_page(
        request.GET.get('page')
    )
    return render(
        request,
        'checklists/director/tasks.html',
        {
            'portal': 'system_admin',
            'store': None,
            'page': page,
            'section_choices': StoreAdHocTask.SectionCode.choices,
            'status_choices': StoreAdHocTask.Status.choices,
            'source_choices': StoreAdHocTask.Source.choices,
            'filters': request.GET,
            'querystring': _querystring_without_page(request),
            'task_admin_scope': True,
        },
    )


@system_admin_required
def system_task_edit(request, task_id):
    task = get_object_or_404(StoreAdHocTask.objects.select_related('store'), pk=task_id)
    store_queryset = Store.objects.filter(
        Q(is_active=True) | Q(pk=task.store_id)
    ).distinct().order_by('name')
    form = StoreAdHocTaskForm(
        request.POST or None,
        initial=StoreAdHocTaskForm.initial_from_task(task),
        store_queryset=store_queryset,
    )
    if request.method == 'POST' and form.is_valid():
        try:
            update_ad_hoc_task(
                task,
                data=form.cleaned_data,
                actor=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Задача изменена.')
            return redirect('checklists:system_tasks')
    return render(
        request,
        'checklists/director/task_form.html',
        {
            'portal': 'system_admin',
            'store': task.store,
            'form': form,
            'title': 'Изменить задачу',
            'task': task,
            'cancel_url': reverse('checklists:system_tasks'),
            'copy_url': reverse(
                'checklists:system_task_copy',
                args=[task.pk],
            ),
        },
    )


@system_admin_required
def system_task_copy(request, task_id):
    task = get_object_or_404(
        StoreAdHocTask.objects.select_related('store'),
        pk=task_id,
    )
    form = StoreAdHocTaskCopyForm(
        request.POST or None,
        source_store=task.store,
        initial={'date': task.date},
    )
    if request.method == 'POST' and form.is_valid():
        try:
            copied_task = copy_ad_hoc_task(
                task,
                target_store=form.cleaned_data['target_store'],
                date=form.cleaned_data['date'],
                actor=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Копия задачи создана.')
            return redirect(
                'checklists:system_task_edit',
                task_id=copied_task.pk,
            )
    return render(
        request,
        'checklists/system_admin/task_copy.html',
        {
            'portal': 'system_admin',
            'store': task.store,
            'task': task,
            'form': form,
            'cancel_url': reverse(
                'checklists:system_task_edit',
                args=[task.pk],
            ),
        },
    )


@system_admin_required
def system_task_delete(request, task_id):
    task = get_object_or_404(
        StoreAdHocTask.objects.select_related('created_by', 'store'),
        pk=task_id,
    )
    if request.method == 'POST':
        delete_ad_hoc_task(
            task,
            actor=request.user,
            request_metadata=_request_metadata(request),
        )
        messages.success(request, 'Задача удалена.')
        return redirect('checklists:system_tasks')
    return render(
        request,
        'checklists/director/task_confirm_delete.html',
        {
            'portal': 'system_admin',
            'store': task.store,
            'task': task,
            'cancel_url': reverse('checklists:system_tasks'),
        },
    )


@system_admin_required
def system_stores(request):
    query = Store.objects.annotate(
        director_count=Count('employees', filter=Q(employees__role=EmployeeProfile.Role.STORE_DIRECTOR)),
    ).order_by('name')
    page = Paginator(query, 25).get_page(request.GET.get('page'))
    return render(
        request,
        'checklists/system_admin/stores.html',
        {
            'portal': 'system_admin',
            'page': page,
            'querystring': _querystring_without_page(request),
        },
    )


@system_admin_required
def system_store_add(request):
    form = StoreCreateForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        try:
            store = create_store_with_defaults(
                actor=request.user,
                name=form.cleaned_data['name'],
                code=form.cleaned_data['code'],
                timezone_name=form.cleaned_data['timezone'],
                is_active=form.cleaned_data['is_active'],
                logo=form.cleaned_data.get('logo'),
                terminal_username=form.cleaned_data.get('terminal_username'),
                terminal_password=form.cleaned_data.get('terminal_password'),
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Магазин и базовые настройки созданы.')
            return redirect('checklists:system_store_detail', store_id=store.pk)
    return render(request, 'checklists/portal_form.html', {'portal': 'system_admin', 'form': form, 'title': 'Новый магазин'})


@system_admin_required
def system_store_detail(request, store_id):
    store = get_object_or_404(Store, pk=store_id)
    directors = EmployeeProfile.objects.filter(
        store=store,
        role=EmployeeProfile.Role.STORE_DIRECTOR,
    ).select_related('user')
    terminal = StoreTerminalAccount.objects.filter(store=store).select_related('user').first()
    return render(
        request,
        'checklists/system_admin/store_detail.html',
        {'portal': 'system_admin', 'store': store, 'directors': directors, 'terminal': terminal},
    )


@system_admin_required
def system_store_edit(request, store_id):
    store = get_object_or_404(Store, pk=store_id)
    form = StoreForm(request.POST or None, request.FILES or None, instance=store)
    if request.method == 'POST' and form.is_valid():
        try:
            update_store(store, form.cleaned_data, request.user, _request_metadata(request))
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Магазин изменён.')
            return redirect('checklists:system_store_detail', store_id=store.pk)
    return render(request, 'checklists/portal_form.html', {'portal': 'system_admin', 'form': form, 'title': 'Изменить магазин'})


def _system_store_activity(request, store_id, active):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    store = get_object_or_404(Store, pk=store_id)
    update_store(store, {'is_active': active}, request.user, _request_metadata(request))
    messages.success(request, 'Статус магазина изменён.')
    return redirect('checklists:system_store_detail', store_id=store.pk)


@system_admin_required
def system_store_activate(request, store_id):
    return _system_store_activity(request, store_id, True)


@system_admin_required
def system_store_deactivate(request, store_id):
    return _system_store_activity(request, store_id, False)


@system_admin_required
def system_admin_store_delete(request, store_id):
    store = Store.objects.filter(pk=store_id).first()
    if store is None:
        already_deleted = AuditLog.objects.filter(
            store__isnull=True,
            object_type=Store._meta.label_lower,
            object_id=str(store_id),
            action=AuditLog.Action.STORE_DELETED,
        ).exists()
        if request.method == 'POST' and already_deleted:
            messages.info(request, 'Магазин уже удалён.')
            return redirect('checklists:system_stores')
        raise Http404
    if request.method == 'POST':
        try:
            result = delete_store_safely(
                actor=request.user,
                store=store,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            messages.error(request, str(exc))
        else:
            if result['method'] == 'hard_delete_with_audit_cleanup':
                messages.success(
                    request,
                    'Магазин удалён. Удалено записей журнала: '
                    f"{result['deleted_audit_entries_count']}.",
                )
            elif result['method'] == 'already_deactivated':
                messages.info(
                    request,
                    'Магазин уже деактивирован, история сохранена.',
                )
            else:
                messages.success(
                    request,
                    'Магазин деактивирован. Пользовательский доступ '
                    'отключён, история сохранена.',
                )
        return redirect('checklists:system_stores')
    return render(
        request,
        'checklists/system_admin/store_confirm_delete.html',
        {
            'portal': 'system_admin',
            'store': store,
            'summary': get_store_deletion_summary(store),
        },
    )


@system_admin_required
def system_store_open_director(request, store_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Выбор магазина выполняется через POST.')
    store = get_object_or_404(Store, pk=store_id, is_active=True)
    set_managed_store(request, store)
    return redirect('checklists:director_dashboard')


@system_admin_required
def system_select_managed_store(request):
    if request.method != 'POST':
        return HttpResponseForbidden('Выбор магазина выполняется через POST.')
    store = get_object_or_404(
        Store,
        pk=request.POST.get('store'),
        is_active=True,
    )
    set_managed_store(request, store)
    target = request.POST.get('next', '')
    if target.startswith('/') and not target.startswith('//'):
        return redirect(target)
    return redirect('checklists:system_admin_dashboard')


@system_admin_required
def system_users(request):
    query = User.objects.select_related(
        'employee_profile__store'
    ).order_by('username')
    role = request.GET.get('role')
    store_id = request.GET.get('store')
    if role == 'superuser':
        query = query.filter(is_superuser=True)
    elif role in EmployeeProfile.Role.values:
        query = query.filter(employee_profile__role=role)
    if store_id:
        query = query.filter(employee_profile__store_id=store_id)
    page = Paginator(query, 25).get_page(request.GET.get('page'))
    return render(
        request,
        'checklists/system_admin/users.html',
        {
            'portal': 'system_admin',
            'page': page,
            'stores': Store.objects.order_by('name'),
            'roles': (
                ('superuser', 'Главный администратор'),
                (EmployeeProfile.Role.SYSTEM_ADMIN, 'Администратор системы'),
                (EmployeeProfile.Role.STORE_DIRECTOR, 'Директор магазина'),
                (EmployeeProfile.Role.STORE_ACCOUNT, 'Сотрудник'),
            ),
            'selected_role': role,
            'selected_store': store_id,
            'querystring': _querystring_without_page(request),
        },
    )


@system_admin_required
def system_user_add(request):
    form = ManagedUserCreateForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            user = create_managed_user(
                actor=request.user,
                username=form.cleaned_data['username'],
                password=form.cleaned_data['password'],
                first_name=form.cleaned_data['first_name'],
                last_name=form.cleaned_data['last_name'],
                email=form.cleaned_data['email'],
                role=form.cleaned_data['role'],
                store=form.cleaned_data['store'],
                is_active=form.cleaned_data['is_active'],
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Пользователь создан.')
            return redirect('checklists:system_user_detail', user_id=user.pk)
    return render(request, 'checklists/portal_form.html', {'portal': 'system_admin', 'form': form, 'title': 'Новый пользователь'})


@system_admin_required
def system_user_detail(request, user_id):
    user = get_object_or_404(
        User.objects.select_related(
            'employee_profile__store',
            'telegram_profile',
        ).prefetch_related('store_memberships__store'),
        pk=user_id,
    )
    return render(
        request,
        'checklists/system_admin/user_detail.html',
        {
            'portal': 'system_admin',
            'managed_user': user,
            'protected_user': (
                user.is_superuser or user.username.casefold() == 'bud'
            ),
            'memberships': user.store_memberships.select_related(
                'store'
            ).order_by('store__name'),
            'membership_form': UserStoreMembershipForm(user=user),
        },
    )


@system_admin_required
def system_user_membership_add(request, user_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    user = get_object_or_404(User, pk=user_id)
    form = UserStoreMembershipForm(request.POST, user=user)
    if form.is_valid():
        try:
            set_user_store_membership(
                user=user,
                store=form.cleaned_data['store'],
                role_in_store=form.cleaned_data['role_in_store'],
                is_active=form.cleaned_data['is_active'],
                actor=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Магазин добавлен пользователю.')
    else:
        messages.error(request, 'Проверьте магазин и роль.')
    return redirect('checklists:system_user_detail', user_id=user.pk)


@system_admin_required
def system_user_membership_remove(request, user_id, membership_id):
    if request.method != 'POST':
        return HttpResponseForbidden('Действие доступно только через POST.')
    membership = get_object_or_404(
        UserStoreMembership,
        pk=membership_id,
        user_id=user_id,
    )
    try:
        remove_user_store_membership(
            membership=membership,
            actor=request.user,
            request_metadata=_request_metadata(request),
        )
    except (ChecklistServiceError, ValidationError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Связь с магазином удалена.')
    return redirect('checklists:system_user_detail', user_id=user_id)


@system_admin_required
def system_user_edit(request, user_id):
    user = get_object_or_404(User.objects.select_related('employee_profile__store'), pk=user_id, employee_profile__isnull=False)
    form = ManagedUserUpdateForm(request.POST or None, initial=managed_user_initial(user))
    if request.method == 'POST' and form.is_valid():
        try:
            update_managed_user(user, form.cleaned_data, request.user, _request_metadata(request))
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Пользователь изменён.')
            return redirect('checklists:system_user_detail', user_id=user.pk)
    return render(request, 'checklists/portal_form.html', {'portal': 'system_admin', 'form': form, 'title': 'Изменить пользователя'})


@system_admin_required
def system_user_delete(request, user_id):
    user = get_object_or_404(
        User.objects.select_related('employee_profile__store'),
        pk=user_id,
    )
    if user.is_superuser or user.username.casefold() == 'bud':
        return HttpResponseForbidden('Главного администратора удалить нельзя.')
    if request.method == 'POST':
        try:
            delete_managed_user(
                user,
                request.user,
                _request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Пользователь удалён.')
            return redirect('checklists:system_users')
    return render(
        request,
        'checklists/system_admin/user_confirm_delete.html',
        {'portal': 'system_admin', 'managed_user': user},
    )


@system_admin_required
def system_user_reset_password(request, user_id):
    user = get_object_or_404(User, pk=user_id, employee_profile__isnull=False)
    form = PasswordResetForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            reset_managed_user_password(
                user,
                form.cleaned_data['password'],
                request.user,
                _request_metadata(request),
                request.session.session_key,
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(request, 'Пароль сброшен; другие сессии завершены.')
            return redirect('checklists:system_user_detail', user_id=user.pk)
    return render(request, 'checklists/portal_form.html', {'portal': 'system_admin', 'form': form, 'title': f'Сброс пароля: {user.username}'})


def _system_user_activity(request, user_id, active):
    if request.method != 'POST':
        return HttpResponseForbidden('Только POST.')
    user = get_object_or_404(User, pk=user_id, employee_profile__isnull=False)
    try:
        if active:
            activate_managed_user(user, request.user, _request_metadata(request))
        else:
            deactivate_managed_user(user, request.user, _request_metadata(request))
    except (ChecklistServiceError, ValidationError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Статус пользователя изменён.')
    return redirect('checklists:system_user_detail', user_id=user.pk)


@system_admin_required
def system_user_activate(request, user_id):
    return _system_user_activity(request, user_id, True)


@system_admin_required
def system_user_deactivate(request, user_id):
    return _system_user_activity(request, user_id, False)


@system_admin_required
def system_audit(request):
    query = AuditLog.objects.select_related('actor', 'store').order_by('-created_at')
    store_id = request.GET.get('store')
    selected_store = None
    if store_id:
        selected_store = get_object_or_404(Store, pk=store_id)
        query = query.filter(store=selected_store)
    page = Paginator(query, 50).get_page(request.GET.get('page'))
    return render(
        request,
        'checklists/system_admin/audit.html',
        {
            'portal': 'system_admin',
            'page': page,
            'stores': Store.objects.order_by('name'),
            'selected_store': store_id,
            'selected_store_obj': selected_store,
            'querystring': _querystring_without_page(request),
        },
    )


def _audit_confirmation_summary(query):
    return query.aggregate(
        entries_count=Count('pk'),
        first_created_at=Min('created_at'),
        last_created_at=Max('created_at'),
    )


@system_admin_required
def system_admin_store_audit_clear(request, store_id):
    store = get_object_or_404(Store, pk=store_id)
    expected_phrase = 'ОЧИСТИТЬ'
    form = AuditClearConfirmationForm(
        request.POST or None,
        expected_phrase=expected_phrase,
    )
    if request.method == 'POST' and form.is_valid():
        try:
            result = clear_store_audit_log(
                actor=request.user,
                store=store,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            if result['method'] == 'already_empty':
                messages.info(request, 'Журнал магазина уже пуст.')
            else:
                messages.success(
                    request,
                    'Журнал магазина очищен. Удалено записей: '
                    f"{result['deleted_entries_count']}.",
                )
            return redirect('checklists:system_audit')

    summary = _audit_confirmation_summary(AuditLog.objects.filter(store=store))
    return render(
        request,
        'checklists/system_admin/audit_confirm_clear.html',
        {
            'portal': 'system_admin',
            'mode': 'store',
            'store': store,
            'summary': summary,
            'form': form,
            'expected_phrase': expected_phrase,
        },
    )


@system_admin_required
def system_admin_audit_clear_all(request):
    expected_phrase = 'ОЧИСТИТЬ ВЕСЬ ЖУРНАЛ'
    form = AuditClearConfirmationForm(
        request.POST or None,
        expected_phrase=expected_phrase,
    )
    if request.method == 'POST' and form.is_valid():
        try:
            result = clear_all_audit_logs(
                actor=request.user,
                request_metadata=_request_metadata(request),
            )
        except (ChecklistServiceError, ValidationError) as exc:
            _service_form_error(form, exc)
        else:
            messages.success(
                request,
                'Системный журнал очищен. Удалено записей: '
                f"{result['deleted_entries_count']}.",
            )
            return redirect('checklists:system_audit')

    query = AuditLog.objects.all()
    summary = _audit_confirmation_summary(query)
    summary['stores_count'] = query.exclude(store=None).values(
        'store_id'
    ).distinct().count()
    summary['global_entries_count'] = query.filter(store=None).count()
    return render(
        request,
        'checklists/system_admin/audit_confirm_clear.html',
        {
            'portal': 'system_admin',
            'mode': 'all',
            'summary': summary,
            'form': form,
            'expected_phrase': expected_phrase,
        },
    )
