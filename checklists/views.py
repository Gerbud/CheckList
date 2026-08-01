from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone

from checklists.exceptions import (
    ChecklistServiceError,
    DuplicateDailyChecklistError,
)
from checklists.forms import DailyChecklistAnswersForm
from checklists.models import (
    ChecklistAnswer,
    ChecklistItem,
    DailyChecklist,
    DailyChecklistStage,
    DailyShiftAssignment,
    EmployeeProfile,
    StoreEmployee,
    StoreTerminalAccount,
)
from checklists.services import (
    can_complete_stage,
    complete_checklist_stage,
    create_daily_checklist,
    get_current_stage,
    get_stage_completion_available_at,
    get_stage_state,
    get_stage_warning_minutes,
    get_shift_completion_report,
    update_answer,
)


PROFILE_ERROR_MESSAGE = (
    'Для пользователя не настроен профиль сотрудника или терминальный аккаунт. '
    'Обратитесь к руководителю.'
)
SELECTED_EMPLOYEE_SESSION_KEY = 'selected_store_employee_id'


def _account_context(user):
    try:
        return StoreTerminalAccount.objects.select_related('store').get(
            user=user,
            is_active=True,
            store__is_active=True,
        )
    except StoreTerminalAccount.DoesNotExist:
        pass
    try:
        profile = EmployeeProfile.objects.select_related('store').get(
            user=user,
            is_active=True,
            store__is_active=True,
        )
    except EmployeeProfile.DoesNotExist:
        return None
    return profile


def _is_terminal(account):
    return isinstance(account, StoreTerminalAccount)


def _daily_account_filter(account):
    if _is_terminal(account):
        return {'terminal_account': account}
    return {'employee': account}


def _selected_employee(request, account):
    if not _is_terminal(account):
        request.session.pop(SELECTED_EMPLOYEE_SESSION_KEY, None)
        return None
    employee_id = request.session.get(SELECTED_EMPLOYEE_SESSION_KEY)
    if not employee_id:
        return None
    employee = StoreEmployee.objects.filter(
        pk=employee_id,
        store=account.store,
        is_active=True,
    ).first()
    if employee is None:
        request.session.pop(SELECTED_EMPLOYEE_SESSION_KEY, None)
    return employee


def _safe_next_url(request, value, default='checklists:dashboard'):
    if value and url_has_allowed_host_and_scheme(
        value,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return value
    return reverse(default)


def _store_timezone(store):
    try:
        return ZoneInfo(store.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo('UTC')


def _store_today(store):
    return timezone.now().astimezone(_store_timezone(store)).date()


def _summary(daily):
    if daily is None:
        return {'completed': 0, 'failed': 0, 'not_applicable': 0, 'pending': 0}
    statuses = list(
        ChecklistAnswer.objects.filter(
            daily_item__daily_checklist=daily
        ).values_list('status', flat=True)
    )
    return {
        status: statuses.count(status)
        for status in ChecklistAnswer.Status.values
    }


STAGE_URL_NAMES = {
    DailyChecklistStage.SectionCode.OPENING: 'checklists:opening',
    DailyChecklistStage.SectionCode.DURING_DAY: 'checklists:during_day',
    DailyChecklistStage.SectionCode.CLOSING: 'checklists:closing',
}


def _answer_has_value(answer):
    if (
        answer.daily_item.answer_type_snapshot
        == ChecklistItem.AnswerType.INTEGER
    ):
        return answer.integer_value is not None
    return answer.status not in {
        None,
        ChecklistAnswer.Status.PENDING,
    }


def _stage_context(stage, store, now):
    state = get_stage_state(stage, now)
    completion_available_at = get_stage_completion_available_at(stage)
    completion_allowed = can_complete_stage(stage, now)
    store_tz = _store_timezone(store)
    opens_local = stage.opens_at.astimezone(store_tz)
    deadline_local = stage.deadline_at.astimezone(store_tz)
    completion_available_local = completion_available_at.astimezone(store_tz)
    warning_minutes = get_stage_warning_minutes(stage)
    warning_at = stage.deadline_at - timedelta(minutes=warning_minutes)
    stage_answers = list(
        ChecklistAnswer.objects.filter(
            daily_item__daily_checklist=stage.daily_checklist,
            daily_item__section_code=stage.section_code,
        ).select_related('daily_item')
    )
    completed = state in {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }
    overdue = state == DailyChecklistStage.Status.OVERDUE
    has_saved_answers = any(
        answer.answered_at is not None for answer in stage_answers
    )
    total_questions = len(stage_answers)
    answered_questions = sum(
        _answer_has_value(answer) for answer in stage_answers
    )
    progress_percent = (
        round(answered_questions * 100 / total_questions)
        if total_questions
        else 0
    )
    if completed:
        employee_state_label = 'Завершён'
    elif overdue:
        employee_state_label = 'Просрочен'
    elif total_questions and answered_questions == total_questions:
        employee_state_label = 'Готов к завершению'
    elif answered_questions:
        employee_state_label = 'Заполнено частично'
    elif has_saved_answers:
        employee_state_label = 'Есть черновик'
    else:
        employee_state_label = 'Не начат'
    return {
        'stage': stage,
        'state': state,
        'state_label': employee_state_label,
        'url': reverse(STAGE_URL_NAMES[stage.section_code]),
        'opens_local': opens_local,
        'opens_time': opens_local.strftime('%H:%M'),
        'deadline_local': deadline_local,
        'deadline_time': deadline_local.strftime('%H:%M'),
        'completion_available_local': completion_available_local,
        'completion_available_time': (
            completion_available_local.strftime('%H:%M')
        ),
        'completed_local': stage.completed_at.astimezone(store_tz)
        if stage.completed_at
        else None,
        'is_locked': state == DailyChecklistStage.Status.LOCKED,
        'is_completed': completed,
        'is_overdue': overdue,
        'can_complete': completion_allowed,
        'has_saved_answers': has_saved_answers,
        'answered_questions': answered_questions,
        'total_questions': total_questions,
        'progress_percent': progress_percent,
        'is_soon': state == DailyChecklistStage.Status.AVAILABLE
        and now >= warning_at,
        'warning_at': warning_at,
        'completed_count': answered_questions,
        'remaining_count': total_questions - answered_questions,
    }


def _profile_missing(request):
    return render(
        request,
        'checklists/profile_missing.html',
        {'profile_error_message': PROFILE_ERROR_MESSAGE},
        status=403,
    )


@login_required
def select_store_employee(request):
    account = _account_context(request.user)
    if account is None:
        return _profile_missing(request)
    if not _is_terminal(account):
        return HttpResponseForbidden(
            'Выбор сотрудника доступен только на терминале.'
        )
    next_url = _safe_next_url(
        request,
        request.POST.get('next') or request.GET.get('next'),
    )
    if request.method == 'POST':
        employee_id = request.POST.get('employee_id')
        employee = StoreEmployee.objects.filter(
            pk=employee_id,
            store=account.store,
            is_active=True,
        ).first()
        if employee is None:
            return HttpResponseForbidden('Недоступный сотрудник.')
        request.session[SELECTED_EMPLOYEE_SESSION_KEY] = employee.pk
        return redirect(next_url)
    employees = StoreEmployee.objects.filter(
        store=account.store,
        is_active=True,
    ).order_by('sort_order', 'display_name', 'id')
    work_date = _store_today(account.store)
    assigned_ids = set(
        DailyShiftAssignment.objects.filter(
            store=account.store,
            work_date=work_date,
        ).values_list('employee_id', flat=True)
    )
    employee_cards = [
        {'employee': employee, 'is_assigned': employee.pk in assigned_ids}
        for employee in employees
    ]
    return render(
        request,
        'checklists/select_employee.html',
        {
            'profile': account,
            'employees': employee_cards,
            'next_url': next_url,
            'selected_employee': _selected_employee(request, account),
        },
    )


@login_required
def change_store_employee(request):
    if request.method != 'POST':
        return HttpResponseForbidden('Смена сотрудника выполняется через POST.')
    account = _account_context(request.user)
    if account is None:
        return _profile_missing(request)
    if not _is_terminal(account):
        return HttpResponseForbidden(
            'Смена сотрудника доступна только на терминале.'
        )
    request.session.pop(SELECTED_EMPLOYEE_SESSION_KEY, None)
    next_url = _safe_next_url(request, request.POST.get('next'))
    return redirect(f"{reverse('checklists:select_employee')}?next={next_url}")


@login_required
def dashboard(request):
    account = _account_context(request.user)
    if account is None:
        return _profile_missing(request)
    checklist_date = _store_today(account.store)
    daily = (
        DailyChecklist.objects.filter(
            store=account.store,
            checklist_date=checklist_date,
            **_daily_account_filter(account),
        )
        .select_related('template_version')
        .prefetch_related('stages')
        .first()
    )
    service_error = None
    if daily is None:
        try:
            daily = create_daily_checklist(account, checklist_date)
        except DuplicateDailyChecklistError:
            daily = DailyChecklist.objects.prefetch_related('stages').get(
                store=account.store,
                checklist_date=checklist_date,
                **_daily_account_filter(account),
            )
        except ChecklistServiceError as exc:
            service_error = str(exc)
    now = timezone.now()
    stages = [
        _stage_context(stage, account.store, now)
        for stage in daily.stages.all().order_by('opens_at', 'id')
    ] if daily else []
    shift_report = get_shift_completion_report(account.store, checklist_date)
    return render(
        request,
        'checklists/dashboard.html',
        {
            'profile': account,
            'is_terminal': _is_terminal(account),
            'selected_employee': _selected_employee(request, account),
            'employee_name': request.user.get_full_name() or request.user.get_username(),
            'checklist_date': checklist_date,
            'daily': daily,
            'summary': _summary(daily),
            'stages': stages,
            'server_now': now.isoformat(),
            'now_local': now.astimezone(_store_timezone(account.store)),
            'service_error': service_error,
            'shift_report': shift_report,
        },
    )


def _request_metadata(request):
    return {
        'ip_address': request.META.get('REMOTE_ADDR'),
        'user_agent': request.META.get('HTTP_USER_AGENT'),
    }


def _answer_sections(answers, form):
    sections = []
    current = None
    for answer in answers:
        item = answer.daily_item
        if current is None or current['code'] != item.section_code:
            current = {
                'code': item.section_code,
                'name': item.section_name,
                'rows': [],
            }
            sections.append(current)
        row = {
            'answer': answer,
            'item': item,
            'reason_field': form[form.reason_field_name(answer)],
            'revisions': list(answer.revisions.all()),
        }
        if item.answer_type_snapshot == ChecklistItem.AnswerType.INTEGER:
            row['integer_field'] = form[form.integer_field_name(answer)]
        else:
            row['status_field'] = form[form.status_field_name(answer)]
            row['comment_field'] = form[form.comment_field_name(answer)]
        current['rows'].append(row)
    return sections


def _daily_context(
    profile,
    daily,
    stage,
    form,
    now,
    selected_employee=None,
):
    answers = list(
        ChecklistAnswer.objects.filter(
            daily_item__daily_checklist=daily,
            daily_item__section_code=stage.section_code,
        )
        .select_related(
            'daily_item',
            'answered_by_employee',
            'last_edited_by_employee',
        )
        .prefetch_related('revisions__changed_by_employee')
        .order_by(
            'daily_item__section_sort_order',
            'daily_item__display_order',
            'daily_item__id',
        )
    )
    completed_local = None
    if daily.completed_at:
        completed_local = daily.completed_at.astimezone(_store_timezone(profile.store))
    return {
        'profile': profile,
        'daily': daily,
        'stage_context': _stage_context(stage, profile.store, now),
        'server_now': now.isoformat(),
        'form': form,
        'sections': _answer_sections(answers, form),
        'summary': _summary(daily),
        'completed_local': completed_local,
        'selected_employee': selected_employee,
        'is_terminal': _is_terminal(profile),
    }


@login_required
def today_checklist(request):
    profile = _account_context(request.user)
    if profile is None:
        return _profile_missing(request)
    checklist_date = _store_today(profile.store)
    daily = DailyChecklist.objects.filter(
        store=profile.store,
        checklist_date=checklist_date,
        **_daily_account_filter(profile),
    ).first()
    if daily is None:
        try:
            daily = create_daily_checklist(profile, checklist_date)
        except DuplicateDailyChecklistError:
            daily = DailyChecklist.objects.get(
                store=profile.store,
                checklist_date=checklist_date,
                **_daily_account_filter(profile),
            )
        except ChecklistServiceError as exc:
            return render(
                request,
                'checklists/daily_checklist.html',
                {'daily': None, 'service_error': str(exc), 'profile': profile},
                status=409,
            )

    current_stage = get_current_stage(daily, timezone.now())
    if current_stage is None:
        return render(
            request,
            'checklists/daily_checklist.html',
            {
                'daily': None,
                'service_error': 'Для чек-листа не созданы временные этапы.',
                'profile': profile,
            },
            status=409,
        )
    return redirect(STAGE_URL_NAMES[current_stage.section_code])


@login_required
def checklist_stage(request, section_code):
    profile = _account_context(request.user)
    if profile is None:
        return _profile_missing(request)
    checklist_date = _store_today(profile.store)
    daily = DailyChecklist.objects.filter(
        store=profile.store,
        checklist_date=checklist_date,
        **_daily_account_filter(profile),
    ).first()
    if daily is None:
        try:
            daily = create_daily_checklist(profile, checklist_date)
        except DuplicateDailyChecklistError:
            daily = DailyChecklist.objects.get(
                store=profile.store,
                checklist_date=checklist_date,
                **_daily_account_filter(profile),
            )
        except ChecklistServiceError as exc:
            return render(
                request,
                'checklists/daily_checklist.html',
                {'daily': None, 'service_error': str(exc), 'profile': profile},
                status=409,
            )

    stage = get_object_or_404(
        DailyChecklistStage,
        daily_checklist=daily,
        section_code=section_code,
    )
    now = timezone.now()
    state = get_stage_state(stage, now)

    answers = list(
        ChecklistAnswer.objects.filter(
            daily_item__daily_checklist=daily,
            daily_item__section_code=section_code,
        )
        .select_related('daily_item')
        .order_by(
            'daily_item__section_sort_order',
            'daily_item__display_order',
            'daily_item__id',
        )
    )
    readonly = state in {
        DailyChecklistStage.Status.COMPLETED,
        DailyChecklistStage.Status.COMPLETED_LATE,
    }
    selected_employee = _selected_employee(request, profile)
    if _is_terminal(profile) and not readonly and selected_employee is None:
        if request.method == 'POST':
            return HttpResponseForbidden('Сначала выберите сотрудника.')
        select_url = reverse('checklists:select_employee')
        return redirect(f'{select_url}?next={request.path}')
    if request.method == 'POST' and readonly:
        return render(request, '403.html', status=403)
    if (
        request.method == 'POST'
        and request.POST.get('employee_id')
        and str(selected_employee.pk if selected_employee else '')
        != request.POST.get('employee_id')
    ):
        return render(request, '403.html', status=403)
    if (
        request.method == 'POST'
        and request.POST.get('section_code', section_code) != section_code
    ):
        return render(request, '403.html', status=403)

    action = (
        request.POST.get('action', 'save')
        if request.method == 'POST'
        else 'save'
    )
    form = DailyChecklistAnswersForm(
        request.POST or None,
        answers=answers,
        require_complete=False,
    )
    if request.method == 'POST':
        form_is_valid = form.is_valid()
        if action not in {'save', 'complete_stage'}:
            form.add_error(None, 'Неизвестное действие формы.')
            form_is_valid = False
        if form_is_valid:
            try:
                with transaction.atomic():
                    for (
                        answer,
                        status,
                        comment,
                        integer_value,
                        change_reason,
                    ) in form.updates():
                        update_answer(
                            answer,
                            status,
                            comment,
                            request.user,
                            employee=selected_employee,
                            change_reason=change_reason,
                            request_metadata=_request_metadata(request),
                            integer_value=integer_value,
                        )
            except ChecklistServiceError as exc:
                form.add_error(None, str(exc))
            else:
                if action == 'complete_stage':
                    try:
                        complete_checklist_stage(
                            stage,
                            request.user,
                            employee=selected_employee,
                            request_metadata=_request_metadata(request),
                        )
                    except ChecklistServiceError as exc:
                        detail = str(exc)
                        if detail:
                            detail = detail[0].lower() + detail[1:]
                        saved_message = (
                            f'Ответы сохранены, но {detail}'
                        )
                        form.add_error(None, str(exc))
                        messages.warning(request, saved_message)
                    else:
                        messages.success(request, 'Этап успешно завершён.')
                        request.session.pop(
                            SELECTED_EMPLOYEE_SESSION_KEY,
                            None,
                        )
                        return redirect('checklists:dashboard')
                else:
                    messages.success(request, 'Ответы сохранены.')
                    return redirect(STAGE_URL_NAMES[section_code])

    return render(
        request,
        'checklists/daily_checklist.html',
        _daily_context(
            profile,
            daily,
            stage,
            form,
            now,
            selected_employee,
        ),
    )


def permission_denied(request, exception=None):
    return render(request, '403.html', status=403)


def page_not_found(request, exception=None):
    return render(request, '404.html', status=404)
