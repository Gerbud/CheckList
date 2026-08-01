import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from checklists.models import (
    AnswerRevision,
    ChecklistAnswer,
    ChecklistDayStatus,
    ChecklistItem,
    ChecklistNotification,
    DailyChecklist,
    DailyChecklistStage,
    DailyShiftAssignment,
    StoreAdHocTask,
    StoreEmployee,
    TelegramOutboundMessage,
)


FINAL_STAGE_STATUSES = {
    DailyChecklistStage.Status.COMPLETED,
    DailyChecklistStage.Status.COMPLETED_LATE,
}
ACTIVE_TASK_STATUSES = {
    StoreAdHocTask.Status.PLANNED,
    StoreAdHocTask.Status.ACTIVE,
}
MAX_REPORT_DAYS = 366


@dataclass(frozen=True)
class ReportPeriod:
    date_from: object
    date_to: object
    previous_from: object
    previous_to: object
    was_limited: bool = False

    @property
    def label(self):
        return f'{self.date_from:%d.%m.%Y}–{self.date_to:%d.%m.%Y}'


def make_report_period(date_from, date_to):
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    was_limited = (date_to - date_from).days + 1 > MAX_REPORT_DAYS
    if was_limited:
        date_from = date_to - timedelta(days=MAX_REPORT_DAYS - 1)
    length = (date_to - date_from).days + 1
    return ReportPeriod(
        date_from=date_from,
        date_to=date_to,
        previous_from=date_from - timedelta(days=length),
        previous_to=date_from - timedelta(days=1),
        was_limited=was_limited,
    )


def _daily_queryset(store, period, *, include_excluded=False):
    query = DailyChecklist.objects.filter(
        store=store,
        checklist_date__range=(period.date_from, period.date_to),
    )
    if not include_excluded:
        query = query.filter(day_status=ChecklistDayStatus.NORMAL)
    return (
        query
        .select_related('terminal_account__user', 'employee__user')
        .prefetch_related(
            'stages__completed_by_employee',
            'stages__notifications',
            'items__answer__answered_by_employee',
            'items__answer__last_edited_by_employee',
            'items__answer__revisions__changed_by_employee',
        )
        .order_by('-checklist_date', 'pk')
    )


def _answer_is_missing(answer):
    if answer.daily_item.answer_type_snapshot == ChecklistItem.AnswerType.INTEGER:
        return answer.integer_value is None
    return answer.status == ChecklistAnswer.Status.PENDING


def _answer_label(answer):
    if answer.daily_item.answer_type_snapshot == ChecklistItem.AnswerType.INTEGER:
        return (
            f'Числовой ответ: {answer.integer_value}'
            if answer.integer_value is not None
            else 'Нет ответа'
        )
    return {
        ChecklistAnswer.Status.COMPLETED: 'Выполнено',
        ChecklistAnswer.Status.FAILED: 'Не выполнено',
        ChecklistAnswer.Status.NOT_APPLICABLE: 'Не применимо',
        ChecklistAnswer.Status.PENDING: 'Нет ответа',
    }.get(answer.status, 'Нет ответа')


def _comparison(current, previous, *, higher_is_better=True):
    if previous is None:
        return {'label': 'Нет данных для сравнения', 'state': 'neutral'}
    if previous == 0:
        if current == 0:
            return {'label': 'Без изменений', 'state': 'neutral'}
        return {
            'label': 'В предыдущем периоде значение было 0',
            'state': 'neutral',
        }
    delta = current - previous
    if delta == 0:
        return {'label': 'Без изменений', 'state': 'neutral'}
    better = delta > 0 if higher_is_better else delta < 0
    direction = 'улучшение' if better else 'ухудшение'
    return {
        'label': f'{direction}: {abs(delta):g}',
        'state': 'good' if better else 'bad',
    }


def collect_store_facts(store, period, *, now=None):
    now = now or timezone.now()
    dailies = list(_daily_queryset(store, period))
    stages = [stage for daily in dailies for stage in daily.stages.all()]
    items = [item for daily in dailies for item in daily.items.all()]
    answers = [item.answer for item in items]
    tasks = list(
        StoreAdHocTask.objects.filter(
            store=store,
            date__range=(period.date_from, period.date_to),
        ).select_related('completed_by_employee', 'created_by')
    )
    assignments = list(
        DailyShiftAssignment.objects.filter(
            store=store,
            work_date__range=(period.date_from, period.date_to),
        ).select_related('employee')
    )
    revisions = [
        revision
        for answer in answers
        for revision in answer.revisions.all()
    ]
    notifications = [
        notification
        for stage in stages
        for notification in stage.notifications.all()
    ]
    outbound_errors = list(
        TelegramOutboundMessage.objects.filter(
            store=store,
            status=TelegramOutboundMessage.Status.FAILED,
            created_at__date__range=(period.date_from, period.date_to),
        )[:100]
    )
    return {
        'dailies': dailies,
        'stages': stages,
        'items': items,
        'answers': answers,
        'tasks': tasks,
        'assignments': assignments,
        'revisions': revisions,
        'notifications': notifications,
        'outbound_errors': outbound_errors,
        'now': now,
    }


def identify_problems(store, period, facts=None):
    facts = facts or collect_store_facts(store, period)
    now = facts['now']
    problems = []
    participants_by_date = defaultdict(set)
    for answer in facts['answers']:
        work_date = answer.daily_item.daily_checklist.checklist_date
        if answer.answered_by_employee_id:
            participants_by_date[work_date].add(answer.answered_by_employee_id)
        if answer.last_edited_by_employee_id:
            participants_by_date[work_date].add(answer.last_edited_by_employee_id)
        if answer.daily_item.is_required and _answer_is_missing(answer):
            problems.append({
                'type': 'missing_answer',
                'severity': 'critical',
                'title': 'Обязательный вопрос без ответа',
                'detail': answer.daily_item.item_text,
                'date': work_date,
                'checklist_id': answer.daily_item.daily_checklist_id,
            })
        if answer.status == ChecklistAnswer.Status.FAILED:
            problems.append({
                'type': 'failed_answer',
                'severity': 'attention',
                'title': 'Обязательное действие не выполнено',
                'detail': answer.daily_item.item_text,
                'date': work_date,
                'checklist_id': answer.daily_item.daily_checklist_id,
            })
            if (
                answer.daily_item.comment_required_on_failure
                and not answer.comment.strip()
            ):
                problems.append({
                    'type': 'missing_comment',
                    'severity': 'critical',
                    'title': 'Нет обязательного комментария к невыполнению',
                    'detail': answer.daily_item.item_text,
                    'date': work_date,
                    'checklist_id': answer.daily_item.daily_checklist_id,
                })
    for stage in facts['stages']:
        work_date = stage.daily_checklist.checklist_date
        if stage.completed_by_employee_id:
            participants_by_date[work_date].add(stage.completed_by_employee_id)
        if stage.status == DailyChecklistStage.Status.COMPLETED_LATE:
            problems.append({
                'type': 'late_stage',
                'severity': 'attention',
                'title': 'Этап завершён после дедлайна',
                'detail': stage.get_section_code_display(),
                'date': work_date,
                'checklist_id': stage.daily_checklist_id,
            })
        elif stage.deadline_at < now and stage.status not in FINAL_STAGE_STATUSES:
            problems.append({
                'type': 'incomplete_stage',
                'severity': 'critical',
                'title': 'Этап не завершён к дедлайну',
                'detail': stage.get_section_code_display(),
                'date': work_date,
                'checklist_id': stage.daily_checklist_id,
            })
    for task in facts['tasks']:
        if task.status == StoreAdHocTask.Status.FAILED or (
            task.date < now.date() and task.status in ACTIVE_TASK_STATUSES
        ):
            problems.append({
                'type': 'overdue_task',
                'severity': (
                    'critical' if task.is_required else 'attention'
                ),
                'title': 'Просроченная или невыполненная задача',
                'detail': task.text,
                'date': task.date,
                'task_id': task.pk,
            })
    for revision in facts['revisions']:
        problems.append({
            'type': 'revision',
            'severity': 'attention',
            'title': 'Ответ изменён после сохранения',
            'detail': revision.answer.daily_item.item_text,
            'date': revision.answer.daily_item.daily_checklist.checklist_date,
            'checklist_id': revision.answer.daily_item.daily_checklist_id,
        })
    for assignment in facts['assignments']:
        if assignment.employee_id not in participants_by_date[assignment.work_date]:
            problems.append({
                'type': 'missing_participation',
                'severity': 'attention',
                'title': 'Сотрудник смены не участвовал',
                'detail': assignment.employee.display_name,
                'date': assignment.work_date,
                'employee_id': assignment.employee_id,
            })
    failed_notifications = [
        item for item in facts['notifications']
        if item.status == ChecklistNotification.Status.FAILED
    ]
    if failed_notifications or facts['outbound_errors']:
        problems.append({
            'type': 'telegram_error',
            'severity': 'attention',
            'title': 'Ошибка Telegram повлияла на уведомления',
            'detail': (
                f'Ошибок: {len(failed_notifications) + len(facts["outbound_errors"])}'
            ),
            'date': period.date_to,
        })
    return sorted(
        problems,
        key=lambda row: (
            row['severity'] != 'critical',
            -row['date'].toordinal(),
            row['title'],
        ),
    )


def calculate_store_health(store, period, facts=None):
    facts = facts or collect_store_facts(store, period)
    problems = identify_problems(store, period, facts)
    critical = [item for item in problems if item['severity'] == 'critical']
    attention = [item for item in problems if item['severity'] == 'attention']
    if critical:
        code, label = 'critical', 'Критично'
        reasons = Counter(item['title'] for item in critical)
    elif attention:
        code, label = 'attention', 'Требует внимания'
        reasons = Counter(item['title'] for item in attention)
    else:
        code, label, reasons = 'normal', 'Нормально', Counter()
    return {
        'code': code,
        'label': label,
        'reasons': [
            f'{title}: {count}' for title, count in reasons.most_common(5)
        ],
        'problems': problems,
    }


def _metric_values(facts):
    stages = facts['stages']
    answers = facts['answers']
    tasks = facts['tasks']
    required = [answer for answer in answers if answer.daily_item.is_required]
    completed_required = [
        answer for answer in required if not _answer_is_missing(answer)
    ]
    completed_stages = [
        stage for stage in stages if stage.status in FINAL_STAGE_STATUSES
    ]
    on_time = [
        stage for stage in stages
        if stage.status == DailyChecklistStage.Status.COMPLETED
    ]
    problem_employees = {
        answer.answered_by_employee_id
        for answer in answers
        if answer.status == ChecklistAnswer.Status.FAILED
        and answer.answered_by_employee_id
    }
    active_tasks = [
        task for task in tasks if task.status in ACTIVE_TASK_STATUSES
    ]
    overdue_tasks = [
        task for task in active_tasks if task.date < facts['now'].date()
    ]
    violated_dates = {
        problem['date']
        for problem in identify_problems(
            facts['dailies'][0].store if facts['dailies'] else (
                tasks[0].store if tasks else None
            ),
            ReportPeriod(
                facts['dailies'][-1].checklist_date if facts['dailies'] else facts['now'].date(),
                facts['dailies'][0].checklist_date if facts['dailies'] else facts['now'].date(),
                facts['now'].date(),
                facts['now'].date(),
            ),
            facts,
        )
    } if (facts['dailies'] or tasks) else set()
    return {
        'completion_rate': round(
            100 * len(completed_required) / len(required)
        ) if required else 100,
        'on_time_rate': round(
            100 * len(on_time) / len(completed_stages)
        ) if completed_stages else 100,
        'late_stages': sum(
            stage.status == DailyChecklistStage.Status.COMPLETED_LATE
            for stage in stages
        ),
        'failed_required': sum(
            answer.daily_item.is_required
            and answer.status == ChecklistAnswer.Status.FAILED
            for answer in answers
        ),
        'active_tasks': len(active_tasks),
        'overdue_tasks': len(overdue_tasks),
        'problem_employees': len(problem_employees),
        'revisions': len(facts['revisions']),
        'clean_days': max(0, len({daily.checklist_date for daily in facts['dailies']}) - len(violated_dates)),
    }


def build_report_dashboard(store, period):
    facts = collect_store_facts(store, period)
    previous = make_report_period(period.previous_from, period.previous_to)
    previous_facts = collect_store_facts(store, previous)
    current_values = _metric_values(facts)
    previous_values = _metric_values(previous_facts)
    health = calculate_store_health(store, period, facts)
    definitions = (
        ('completion_rate', 'Выполнение чек-листов, %', True, '%'),
        ('on_time_rate', 'Этапов завершено вовремя, %', True, '%'),
        ('late_stages', 'Этапов просрочено', False, ''),
        ('failed_required', 'Обязательных вопросов не выполнено', False, ''),
        ('active_tasks', 'Активных задач', False, ''),
        ('overdue_tasks', 'Просроченных задач', False, ''),
        ('problem_employees', 'Сотрудников с проблемами', False, ''),
        ('revisions', 'Изменённых ответов', False, ''),
        ('clean_days', 'Дней без нарушений', True, ''),
    )
    cards = []
    for code, label, higher_is_better, suffix in definitions:
        value = current_values[code]
        state = (
            'critical'
            if code in {'overdue_tasks', 'failed_required'} and value
            else 'attention'
            if code in {'late_stages', 'problem_employees', 'revisions'} and value
            else 'normal'
        )
        cards.append({
            'code': code,
            'label': label,
            'value': f'{value}{suffix}',
            'state': state,
            'state_label': {
                'normal': 'Нормально',
                'attention': 'Требует внимания',
                'critical': 'Критично',
            }[state],
            'comparison': _comparison(
                value,
                previous_values[code],
                higher_is_better=higher_is_better,
            ),
        })
    daily_trend = []
    for daily in sorted(facts['dailies'], key=lambda row: row.checklist_date):
        answers = [item.answer for item in daily.items.all()]
        required = [answer for answer in answers if answer.daily_item.is_required]
        daily_trend.append({
            'date': daily.checklist_date,
            'completion': round(
                100 * sum(not _answer_is_missing(answer) for answer in required)
                / len(required)
            ) if required else 100,
            'failed': sum(
                answer.status == ChecklistAnswer.Status.FAILED
                for answer in answers
            ),
            'late': sum(
                stage.status == DailyChecklistStage.Status.COMPLETED_LATE
                for stage in daily.stages.all()
            ),
        })
    return {
        'health': health,
        'cards': cards,
        'facts': facts,
        'trend': daily_trend,
        'previous_period': previous,
    }


def build_daily_rows(store, period):
    facts = collect_store_facts(store, period)
    history_dailies = list(
        _daily_queryset(store, period, include_excluded=True)
    )
    assignments_by_date = defaultdict(list)
    for assignment in facts['assignments']:
        assignments_by_date[assignment.work_date].append(assignment)
    rows = []
    for daily in history_dailies:
        stage_rows = []
        for stage in daily.stages.all():
            answers = [
                item.answer for item in daily.items.all()
                if item.section_code == stage.section_code
            ]
            answers.sort(
                key=lambda answer: (
                    answer.status != ChecklistAnswer.Status.FAILED,
                    not _answer_is_missing(answer),
                    answer.daily_item.display_order,
                )
            )
            stage_rows.append({
                'stage': stage,
                'answers': [
                    {
                        'answer': answer,
                        'label': _answer_label(answer),
                        'missing': _answer_is_missing(answer),
                        'revision_count': answer.revisions.count(),
                    }
                    for answer in answers
                ],
                'required': sum(
                    answer.daily_item.is_required for answer in answers
                ),
                'completed': sum(
                    answer.daily_item.is_required and not _answer_is_missing(answer)
                    for answer in answers
                ),
                'done': sum(
                    answer.status == ChecklistAnswer.Status.COMPLETED
                    or (
                        answer.daily_item.answer_type_snapshot
                        == ChecklistItem.AnswerType.INTEGER
                        and answer.integer_value is not None
                    )
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
                'missing': sum(
                    answer.daily_item.is_required and _answer_is_missing(answer)
                    for answer in answers
                ),
                'tasks': [
                    task for task in facts['tasks']
                    if task.daily_stage_id == stage.pk
                    or (
                        task.date == daily.checklist_date
                        and {
                            'morning': 'opening',
                            'day': 'during_day',
                            'evening': 'closing',
                        }.get(task.section_code) == stage.section_code
                    )
                ],
                'notifications': list(stage.notifications.all()),
            })
        participants = {
            employee
            for item in daily.items.all()
            for employee in (
                item.answer.answered_by_employee,
                item.answer.last_edited_by_employee,
            )
            if employee
        }
        has_critical = any(
            row['missing']
            or (
                row['stage'].deadline_at < facts['now']
                and row['stage'].status not in FINAL_STAGE_STATUSES
            )
            for row in stage_rows
        )
        has_attention = any(
            row['failed']
            or row['stage'].status == DailyChecklistStage.Status.COMPLETED_LATE
            or any(answer['revision_count'] for answer in row['answers'])
            for row in stage_rows
        )
        excluded_from_statistics = (
            daily.day_status != ChecklistDayStatus.NORMAL
        )
        rows.append({
            'daily': daily,
            'excluded_from_statistics': excluded_from_statistics,
            'stages': stage_rows,
            'assignments': assignments_by_date[daily.checklist_date],
            'participants': sorted(
                participants,
                key=lambda employee: (employee.sort_order, employee.display_name),
            ),
            'problem_count': 0 if excluded_from_statistics else sum(
                row['failed'] + row['missing']
                + int(row['stage'].status == DailyChecklistStage.Status.COMPLETED_LATE)
                for row in stage_rows
            ),
            'done_count': sum(row['done'] for row in stage_rows),
            'failed_count': sum(row['failed'] for row in stage_rows),
            'missing_count': sum(row['missing'] for row in stage_rows),
            'health_code': (
                'normal' if excluded_from_statistics
                else 'critical' if has_critical
                else 'attention' if has_attention
                else 'normal'
            ),
            'health_label': (
                'Не учитывается в статистике'
                if excluded_from_statistics
                else 'Критично' if has_critical
                else 'Требует внимания' if has_attention
                else 'Нормально'
            ),
        })
    return rows


def build_employee_rows(store, period):
    facts = collect_store_facts(store, period)
    assignments_by_employee = defaultdict(set)
    for assignment in facts['assignments']:
        assignments_by_employee[assignment.employee_id].add(assignment.work_date)
    rows = []
    for employee in StoreEmployee.objects.filter(store=store):
        first_answers = [
            answer for answer in facts['answers']
            if answer.answered_by_employee_id == employee.pk
        ]
        edited_answers = [
            answer for answer in facts['answers']
            if answer.last_edited_by_employee_id == employee.pk
        ]
        revisions = [
            revision for revision in facts['revisions']
            if revision.changed_by_employee_id == employee.pk
        ]
        stages = [
            stage for stage in facts['stages']
            if stage.completed_by_employee_id == employee.pk
        ]
        tasks = [
            task for task in facts['tasks']
            if task.completed_by_employee_id == employee.pk
        ]
        participation_dates = {
            answer.daily_item.daily_checklist.checklist_date
            for answer in first_answers + edited_answers
        } | {
            stage.daily_checklist.checklist_date for stage in stages
        }
        assigned_dates = assignments_by_employee[employee.pk]
        missing_shifts = len(assigned_dates - participation_dates)
        failed = sum(
            answer.status == ChecklistAnswer.Status.FAILED
            for answer in first_answers
        )
        overdue_tasks = sum(
            task.status == StoreAdHocTask.Status.FAILED
            or (task.date < facts['now'].date() and task.status in ACTIVE_TASK_STATUSES)
            for task in tasks
        )
        late_actions = sum(
            answer.answered_at
            and answer.answered_at
            > next(
                (
                    stage.deadline_at for stage in answer.daily_item.daily_checklist.stages.all()
                    if stage.section_code == answer.daily_item.section_code
                ),
                answer.answered_at,
            )
            for answer in first_answers
        )
        problem_count = missing_shifts + failed + overdue_tasks + len(revisions) + late_actions
        if missing_shifts >= 2 or overdue_tasks >= 2:
            health = ('critical', 'Критичная ситуация')
        elif problem_count:
            health = ('attention', 'Требует внимания')
        else:
            health = ('normal', 'Без проблем')
        reasons = []
        if missing_shifts:
            reasons.append(f'не участвовал в {missing_shifts} назначенных сменах')
        if failed:
            reasons.append(f'ответов «Не выполнено»: {failed}')
        if overdue_tasks:
            reasons.append(f'невыполненных/просроченных задач: {overdue_tasks}')
        if revisions:
            reasons.append(f'изменений ответов: {len(revisions)}')
        rows.append({
            'employee': employee,
            'shift_count': len(assigned_dates),
            'participated_shifts': len(assigned_dates & participation_dates),
            'participation_rate': round(
                100 * len(assigned_dates & participation_dates) / len(assigned_dates)
            ) if assigned_dates else 0,
            'answers_completed': sum(
                answer.status == ChecklistAnswer.Status.COMPLETED
                or answer.integer_value is not None
                for answer in first_answers
            ),
            'failed': failed,
            'missing_required': missing_shifts,
            'tasks_completed': sum(
                task.status == StoreAdHocTask.Status.COMPLETED for task in tasks
            ),
            'tasks_failed': sum(
                task.status == StoreAdHocTask.Status.FAILED for task in tasks
            ),
            'overdue_tasks': overdue_tasks,
            'revisions': len(revisions),
            'late_actions': late_actions,
            'average_reaction': None,
            'problem_count': problem_count,
            'health_code': health[0],
            'health_label': health[1],
            'reasons': reasons,
            'events': sorted(
                [
                    {
                        'date': answer.daily_item.daily_checklist.checklist_date,
                        'stage': answer.daily_item.section_name,
                        'action': answer.daily_item.item_text,
                        'result': _answer_label(answer),
                        'comment': answer.comment,
                        'checklist_id': answer.daily_item.daily_checklist_id,
                    }
                    for answer in first_answers
                    if answer.status == ChecklistAnswer.Status.FAILED
                ],
                key=lambda row: row['date'],
                reverse=True,
            )[:20],
        })
    return rows


def normalize_task_text(value):
    return re.sub(r'\s+', ' ', (value or '').strip().lower())


def build_recurring_problems(store, period):
    facts = collect_store_facts(store, period)
    failed_questions = Counter(
        answer.daily_item.item_text
        for answer in facts['answers']
        if answer.status == ChecklistAnswer.Status.FAILED
    )
    missing_questions = Counter(
        answer.daily_item.item_text
        for answer in facts['answers']
        if answer.daily_item.is_required and _answer_is_missing(answer)
    )
    task_groups = defaultdict(list)
    for task in facts['tasks']:
        if task.status == StoreAdHocTask.Status.FAILED or (
            task.date < facts['now'].date() and task.status in ACTIVE_TASK_STATUSES
        ):
            task_groups[normalize_task_text(task.text)].append(task)
    late_stages = Counter(
        stage.get_section_code_display()
        for stage in facts['stages']
        if stage.status == DailyChecklistStage.Status.COMPLETED_LATE
    )
    weekdays = Counter(
        item['date'].strftime('%A')
        for item in identify_problems(store, period, facts)
    )
    rows = []
    for category, counter in (
        ('Ответ «Не выполнено»', failed_questions),
        ('Обязательный вопрос без ответа', missing_questions),
        ('Этап закрыт поздно', late_stages),
    ):
        rows.extend({
            'category': category,
            'problem': text,
            'count': count,
        } for text, count in counter.most_common())
    rows.extend({
        'category': 'Невыполненная задача',
        'problem': tasks[0].text,
        'count': len(tasks),
        'last_date': max(task.date for task in tasks),
    } for tasks in task_groups.values())
    return {
        'rows': sorted(rows, key=lambda row: (-row['count'], row['problem'])),
        'weekdays': weekdays.most_common(),
    }


def get_revision_analytics(store, period, filters=None):
    filters = filters or {}
    query = AnswerRevision.objects.filter(
        answer__daily_item__daily_checklist__store=store,
        answer__daily_item__daily_checklist__checklist_date__range=(
            period.date_from,
            period.date_to,
        ),
    ).select_related(
        'answer__daily_item__daily_checklist',
        'changed_by_employee',
        'changed_by_user',
    ).prefetch_related('answer__daily_item__daily_checklist__stages')
    if filters.get('employee'):
        query = query.filter(changed_by_employee=filters['employee'])
    if filters.get('stage'):
        query = query.filter(answer__daily_item__section_code=filters['stage'])
    rows = list(query.order_by('-changed_at', '-pk'))
    for row in rows:
        stage = next(
            (
                item for item in row.answer.daily_item.daily_checklist.stages.all()
                if item.section_code == row.answer.daily_item.section_code
            ),
            None,
        )
        row.after_deadline = bool(stage and row.changed_at > stage.deadline_at)
    if filters.get('only_after_deadline'):
        rows = [row for row in rows if row.after_deadline]
    return {
        'rows': rows,
        'total': len(rows),
        'employees': len({
            row.changed_by_employee_id for row in rows if row.changed_by_employee_id
        }),
        'after_deadline': sum(row.after_deadline for row in rows),
        'failed_to_completed': sum(
            row.previous_status == ChecklistAnswer.Status.FAILED
            and row.new_status == ChecklistAnswer.Status.COMPLETED
            for row in rows
        ),
        'completed_to_failed': sum(
            row.previous_status == ChecklistAnswer.Status.COMPLETED
            and row.new_status == ChecklistAnswer.Status.FAILED
            for row in rows
        ),
        'numeric': sum(
            row.previous_integer_value is not None or row.new_integer_value is not None
            for row in rows
        ),
    }


def get_task_analytics(store, period, filters=None):
    filters = filters or {}
    query = StoreAdHocTask.objects.filter(
        store=store,
        date__range=(period.date_from, period.date_to),
    ).select_related(
        'created_by',
        'created_by_telegram_binding',
        'completed_by_employee',
    )
    for field in ('status', 'source'):
        if filters.get(field):
            query = query.filter(**{field: filters[field]})
    if filters.get('stage'):
        query = query.filter(section_code=filters['stage'])
    rows = list(query)
    now_date = timezone.localdate()
    rows.sort(
        key=lambda task: (
            not (
                task.status == StoreAdHocTask.Status.FAILED
                or (task.date < now_date and task.status in ACTIVE_TASK_STATUSES)
            ),
            task.date,
            task.pk,
        )
    )
    completed = [task for task in rows if task.status == StoreAdHocTask.Status.COMPLETED]
    durations = [
        task.completed_at - task.created_at
        for task in completed if task.completed_at
    ]
    return {
        'rows': rows,
        'created': len(rows),
        'completed': len(completed),
        'failed': sum(task.status == StoreAdHocTask.Status.FAILED for task in rows),
        'cancelled': sum(task.status == StoreAdHocTask.Status.CANCELLED for task in rows),
        'overdue': sum(
            task.date < now_date and task.status in ACTIVE_TASK_STATUSES
            for task in rows
        ),
        'completion_rate': round(100 * len(completed) / len(rows)) if rows else 0,
        'average_completion': (
            sum(durations, timedelta()) / len(durations) if durations else None
        ),
        'without_employee': sum(
            task.status == StoreAdHocTask.Status.COMPLETED
            and task.completed_by_employee_id is None
            for task in rows
        ),
    }
