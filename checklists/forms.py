import re

from django import forms
from django.core.exceptions import ValidationError

from checklists.models import ChecklistAnswer, ChecklistItem


ANSWER_FIELD_PATTERN = re.compile(
    r'^answer_(\d+)_(status|integer_value|comment|change_reason)$'
)


class DailyChecklistAnswersForm(forms.Form):
    def __init__(self, *args, answers, require_complete=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.answers = list(answers)
        self.require_complete = require_complete
        self.answer_by_id = {answer.pk: answer for answer in self.answers}

        for answer in self.answers:
            status_name = self.status_field_name(answer)
            integer_name = self.integer_field_name(answer)
            comment_name = self.comment_field_name(answer)
            reason_name = self.reason_field_name(answer)
            if (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.INTEGER
            ):
                self.fields[integer_name] = forms.IntegerField(
                    label='Укажите количество',
                    min_value=0,
                    required=False,
                    initial=answer.integer_value,
                    widget=forms.NumberInput(
                        attrs={
                            'min': 0,
                            'step': 1,
                            'inputmode': 'numeric',
                        }
                    ),
                )
            else:
                self.fields[status_name] = forms.ChoiceField(
                    label='Статус',
                    choices=ChecklistAnswer.Status.choices,
                    required=False,
                    initial=answer.status,
                )
                self.fields[comment_name] = forms.CharField(
                    label='Комментарий',
                    required=False,
                    strip=False,
                    initial=answer.comment,
                    widget=forms.Textarea(attrs={'rows': 2}),
                )
            self.fields[reason_name] = forms.CharField(
                label='Причина изменения',
                required=False,
                min_length=5,
                widget=forms.Textarea(attrs={'rows': 2}),
            )

    @staticmethod
    def status_field_name(answer):
        return f'answer_{answer.pk}_status'

    @staticmethod
    def comment_field_name(answer):
        return f'answer_{answer.pk}_comment'

    @staticmethod
    def integer_field_name(answer):
        return f'answer_{answer.pk}_integer_value'

    @staticmethod
    def reason_field_name(answer):
        return f'answer_{answer.pk}_change_reason'

    def clean(self):
        cleaned_data = super().clean()
        posted_answer_ids = {
            int(match.group(1))
            for key in self.data
            if (match := ANSWER_FIELD_PATTERN.match(key))
        }
        foreign_ids = posted_answer_ids - self.answer_by_id.keys()
        if foreign_ids:
            raise ValidationError(
                'Форма содержит пункт чужого чек-листа. Данные не сохранены.'
            )

        for answer in self.answers:
            status_name = self.status_field_name(answer)
            integer_name = self.integer_field_name(answer)
            comment_name = self.comment_field_name(answer)
            reason_name = self.reason_field_name(answer)
            is_integer = (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.INTEGER
            )
            if is_integer and (
                status_name in self.data or comment_name in self.data
            ):
                raise ValidationError(
                    'Для числового вопроса нельзя передавать статус или комментарий.'
                )
            if not is_integer and integer_name in self.data:
                raise ValidationError(
                    'Для статусного вопроса нельзя передавать числовое значение.'
                )

            if is_integer:
                integer_value = (
                    cleaned_data.get(integer_name)
                    if integer_name in self.data
                    else answer.integer_value
                )
                cleaned_data[integer_name] = integer_value
                changed = integer_value != answer.integer_value
                if (
                    self.require_complete
                    and answer.daily_item.is_required
                    and integer_value is None
                ):
                    self.add_error(
                        integer_name,
                        'Чтобы завершить этап, укажите количество.',
                    )
                reason = cleaned_data.get(reason_name, '')
                if answer.answered_at and changed and len(reason.strip()) < 5:
                    self.add_error(
                        reason_name,
                        'Укажите причину изменения не короче 5 символов.',
                    )
                continue

            status = cleaned_data.get(status_name) or answer.status
            comment = (
                cleaned_data.get(comment_name, '')
                if comment_name in self.data
                else answer.comment
            )
            cleaned_data[status_name] = status
            cleaned_data[comment_name] = comment
            reason = cleaned_data.get(reason_name, '')
            changed = status != answer.status or comment != answer.comment
            if answer.answered_at and changed and len(reason.strip()) < 5:
                self.add_error(
                    reason_name,
                    'Укажите причину изменения не короче 5 символов.',
                )

            if status == ChecklistAnswer.Status.FAILED and not comment.strip():
                self.add_error(
                    comment_name,
                    'Для невыполненного пункта обязателен комментарий.',
                )
            if (
                status == ChecklistAnswer.Status.NOT_APPLICABLE
                and not answer.daily_item.allow_not_applicable
            ):
                self.add_error(
                    status_name,
                    'Для этого пункта нельзя выбрать «Не применимо».',
                )
            if (
                self.require_complete
                and answer.daily_item.is_required
                and status == ChecklistAnswer.Status.PENDING
            ):
                self.add_error(
                    status_name,
                    'Чтобы завершить чек-лист, ответьте на все пункты.',
                )
        return cleaned_data

    def updates(self):
        if not self.is_valid():
            raise ValueError('Нельзя получить данные из невалидной формы.')
        for answer in self.answers:
            reason = self.cleaned_data[self.reason_field_name(answer)]
            if (
                answer.daily_item.answer_type_snapshot
                == ChecklistItem.AnswerType.INTEGER
            ):
                integer_value = self.cleaned_data[
                    self.integer_field_name(answer)
                ]
                if integer_value != answer.integer_value:
                    yield answer, None, '', integer_value, reason
            else:
                status = self.cleaned_data[self.status_field_name(answer)]
                comment = self.cleaned_data[self.comment_field_name(answer)]
                if status != answer.status or comment != answer.comment:
                    yield answer, status, comment, None, reason
