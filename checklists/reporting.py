from collections import defaultdict

from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from checklists.models import (
    AnswerRevision,
    ChecklistAnswer,
    ChecklistDayStatus,
    ChecklistItem,
    DailyChecklist,
    DailyChecklistStage,
    DailyShiftAssignment,
    StoreEmployee,
)
from checklists.services import get_shift_completion_report


FINAL_STAGE_STATUSES = {
    DailyChecklistStage.Status.COMPLETED,
    DailyChecklistStage.Status.COMPLETED_LATE,
}


def stage_lateness(stage):
    if stage.completed_at and stage.completed_at > stage.deadline_at:
        return stage.completed_at - stage.deadline_at
    return None


def get_stage_daily_summary(store, work_date, at=None):
    at = at or timezone.now()
    if timezone.is_naive(at):
        raise ValueError('Время сводки этапов должно содержать часовой пояс.')
    saved_answers = ChecklistAnswer.objects.filter(
        daily_item__daily_checklist_id=OuterRef('daily_checklist_id'),
        daily_item__section_code=OuterRef('section_code'),
        answered_at__isnull=False,
    )
    rows = (
        DailyChecklistStage.objects.filter(
            daily_checklist__store=store,
            daily_checklist__checklist_date=work_date,
        )
        .annotate(has_draft=Exists(saved_answers))
        .values('section_code')
        .annotate(
            completed=Count(
                'id',
                filter=Q(completed_at__isnull=False),
            ),
            overdue=Count(
                'id',
                filter=Q(
                    completed_at__isnull=True,
                    deadline_at__lte=at,
                ),
            ),
            drafts=Count(
                'id',
                filter=Q(
                    completed_at__isnull=True,
                    deadline_at__gt=at,
                    has_draft=True,
                ),
            ),
            not_started=Count(
                'id',
                filter=Q(
                    completed_at__isnull=True,
                    deadline_at__gt=at,
                    has_draft=False,
                ),
            ),
        )
    )
    values_by_section = {
        row['section_code']: row
        for row in rows
    }
    return [
        {
            'section_code': section_code,
            'label': label,
            'completed': values_by_section.get(
                section_code,
                {},
            ).get('completed', 0),
            'drafts': values_by_section.get(
                section_code,
                {},
            ).get('drafts', 0),
            'not_started': values_by_section.get(
                section_code,
                {},
            ).get('not_started', 0),
            'overdue': values_by_section.get(
                section_code,
                {},
            ).get('overdue', 0),
        }
        for section_code, label in DailyChecklistStage.SectionCode.choices
    ]


def get_daily_report(store, start_date, end_date=None):
    end_date = end_date or start_date
    dailies = list(
        DailyChecklist.objects.filter(
            store=store,
            checklist_date__range=(start_date, end_date),
        )
        .select_related('terminal_account__user', 'employee__user')
        .prefetch_related(
            'stages__completed_by_employee',
            'items__answer__answered_by_employee',
            'items__answer__last_edited_by_employee',
            'items__answer__revisions',
        )
        .order_by('-checklist_date', 'pk')
    )
    result = []
    for daily in dailies:
        answers = [item.answer for item in daily.items.all()]
        assigned = list(
            DailyShiftAssignment.objects.filter(
                store=store,
                work_date=daily.checklist_date,
            ).select_related('employee')
        )
        shift_report = get_shift_completion_report(store, daily.checklist_date)
        participants = {
            employee
            for answer in answers
            for employee in (
                answer.answered_by_employee,
                answer.last_edited_by_employee,
            )
            if employee is not None
        }
        result.append(
            {
                'daily': daily,
                'stages': [
                    {
                        'stage': stage,
                        'lateness': stage_lateness(stage),
                    }
                    for stage in daily.stages.all()
                ],
                'total': len(answers),
                'completed': sum(
                    answer.status == ChecklistAnswer.Status.COMPLETED
                    for answer in answers
                ),
                'failed': sum(
                    answer.status == ChecklistAnswer.Status.FAILED
                    for answer in answers
                ),
                'not_applicable': sum(
                    answer.status == ChecklistAnswer.Status.NOT_APPLICABLE
                    for answer in answers
                ),
                'integer_answers': [
                    answer
                    for answer in answers
                    if answer.daily_item.answer_type_snapshot
                    == ChecklistItem.AnswerType.INTEGER
                ],
                'revisions': sum(answer.revisions.count() for answer in answers),
                'participants': sorted(
                    participants,
                    key=lambda employee: (employee.sort_order, employee.display_name),
                ),
                'assigned': assigned,
                'missing': shift_report['missing'],
            }
        )
    return result


def get_employee_report(store, start_date, end_date):
    employees = list(
        StoreEmployee.objects.filter(store=store).order_by(
            'sort_order',
            'display_name',
        )
    )
    assignments = DailyShiftAssignment.objects.filter(
        store=store,
        work_date__range=(start_date, end_date),
    )
    first_answers = ChecklistAnswer.objects.filter(
        daily_item__daily_checklist__store=store,
        daily_item__daily_checklist__checklist_date__range=(start_date, end_date),
        daily_item__daily_checklist__day_status=ChecklistDayStatus.NORMAL,
    )
    revisions = AnswerRevision.objects.filter(
        answer__daily_item__daily_checklist__store=store,
        answer__daily_item__daily_checklist__checklist_date__range=(
            start_date,
            end_date,
        ),
        answer__daily_item__daily_checklist__day_status=ChecklistDayStatus.NORMAL,
    )
    stages = DailyChecklistStage.objects.filter(
        daily_checklist__store=store,
        daily_checklist__checklist_date__range=(start_date, end_date),
        daily_checklist__day_status=ChecklistDayStatus.NORMAL,
    )
    result = []
    for employee in employees:
        employee_assignments = assignments.filter(employee=employee)
        employee_answers = first_answers.filter(answered_by_employee=employee)
        employee_revisions = revisions.filter(changed_by_employee=employee)
        employee_stages = stages.filter(completed_by_employee=employee)
        participation_dates = set(
            employee_answers.values_list(
                'daily_item__daily_checklist__checklist_date',
                flat=True,
            )
        )
        participation_dates.update(
            employee_revisions.values_list(
                'answer__daily_item__daily_checklist__checklist_date',
                flat=True,
            )
        )
        participation_dates.update(
            employee_stages.values_list(
                'daily_checklist__checklist_date',
                flat=True,
            )
        )
        assignment_dates = set(
            employee_assignments.values_list('work_date', flat=True)
        )
        result.append(
            {
                'employee': employee,
                'shift_dates': sorted(assignment_dates),
                'shift_count': len(assignment_dates),
                'opening_participation': employee_answers.filter(
                    daily_item__section_code='opening'
                ).count()
                + employee_revisions.filter(
                    answer__daily_item__section_code='opening'
                ).count(),
                'during_day_participation': employee_answers.filter(
                    daily_item__section_code='during_day'
                ).count()
                + employee_revisions.filter(
                    answer__daily_item__section_code='during_day'
                ).count(),
                'closing_participation': employee_answers.filter(
                    daily_item__section_code='closing'
                ).count()
                + employee_revisions.filter(
                    answer__daily_item__section_code='closing'
                ).count(),
                'answers_filled': employee_answers.count(),
                'answers_changed': employee_revisions.count(),
                'stages_completed': employee_stages.count(),
                'late_stages_completed': employee_stages.filter(
                    status=DailyChecklistStage.Status.COMPLETED_LATE
                ).count(),
                'days_without_participation': len(
                    assignment_dates - participation_dates
                ),
                'revision_reasons': list(
                    employee_revisions.order_by('-changed_at').values_list(
                        'change_reason',
                        flat=True,
                    )[:20]
                ),
            }
        )
    return result


def get_revision_report(store, start_date, end_date, employee=None, section=None):
    query = AnswerRevision.objects.filter(
        answer__daily_item__daily_checklist__store=store,
        answer__daily_item__daily_checklist__checklist_date__range=(
            start_date,
            end_date,
        ),
    ).select_related(
        'answer__daily_item__daily_checklist__reopened_by',
        'changed_by_employee',
    )
    if employee:
        query = query.filter(changed_by_employee=employee)
    if section:
        query = query.filter(answer__daily_item__section_code=section)
    return query.order_by('-changed_at', '-pk')


def get_director_dashboard_data(store, work_date):
    stage_summary = get_stage_daily_summary(store, work_date)
    daily = (
        DailyChecklist.objects.filter(store=store, checklist_date=work_date)
        .prefetch_related(
            'stages__completed_by_employee',
            'items__answer__answered_by_employee',
        )
        .order_by('pk')
        .first()
    )
    shift_report = get_shift_completion_report(store, work_date)
    if daily is None:
        return {
            'daily': None,
            'stages': [],
            'completed_answers': 0,
            'pending_answers': 0,
            'participants': [],
            'shift_report': shift_report,
            'stage_summary': stage_summary,
        }
    answers = [item.answer for item in daily.items.all()]
    participants = {
        answer.answered_by_employee
        for answer in answers
        if answer.answered_by_employee
    }
    return {
        'daily': daily,
        'stages': [
            {'stage': stage, 'lateness': stage_lateness(stage)}
            for stage in daily.stages.all()
        ],
        'completed_answers': sum(
            (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.INTEGER
                and answer.integer_value is not None
            )
            or (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.STATUS
                and answer.status != ChecklistAnswer.Status.PENDING
            )
            for answer in answers
        ),
        'pending_answers': sum(
            (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.INTEGER
                and answer.integer_value is None
            )
            or (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.STATUS
                and answer.status == ChecklistAnswer.Status.PENDING
            )
            for answer in answers
        ),
        'participants': sorted(
            participants,
            key=lambda employee: (employee.sort_order, employee.display_name),
        ),
        'shift_report': shift_report,
        'stage_summary': stage_summary,
    }
