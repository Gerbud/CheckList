from datetime import date

from django import forms

from warranty.customer_bot import _phone


class WarrantyServiceForm(forms.Form):
    FLOW_REGISTRATION = 'registration'
    FLOW_CLAIM = 'claim'
    FLOW_CHOICES = ((FLOW_REGISTRATION, 'Активировать гарантию'), (FLOW_CLAIM, 'Оформить обращение'))

    flow = forms.ChoiceField(choices=FLOW_CHOICES, widget=forms.HiddenInput)
    full_name = forms.CharField(max_length=255, label='Фамилия, имя и отчество')
    phone = forms.CharField(max_length=64, label='Телефон')
    article = forms.CharField(max_length=255, label='Артикул товара')
    serial_number = forms.CharField(max_length=255, label='Серийный номер')
    purchase_date = forms.DateField(label='Дата покупки', widget=forms.DateInput(attrs={'type': 'date'}))
    label_photo = forms.ImageField(label='Фото этикетки на товаре')
    receipt_photo = forms.ImageField(label='Фото чека')
    warranty_card_photo = forms.ImageField(label='Фото гарантийного талона', required=False)
    defect = forms.CharField(label='Что случилось?', required=False, max_length=2000, widget=forms.Textarea(attrs={'rows': 4}))
    consent = forms.BooleanField(label='Согласие на обработку персональных данных')

    def clean_phone(self):
        normalized = _phone(self.cleaned_data['phone'])
        if not normalized:
            raise forms.ValidationError('Введите российский номер, например +7 999 123-45-67.')
        return normalized

    def clean_full_name(self):
        value = ' '.join(self.cleaned_data['full_name'].split())
        if len(value.split()) < 2:
            raise forms.ValidationError('Укажите фамилию и имя.')
        return value

    def clean_purchase_date(self):
        value = self.cleaned_data['purchase_date']
        if value > date.today():
            raise forms.ValidationError('Дата покупки не может быть в будущем.')
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('flow') == self.FLOW_CLAIM:
            if not cleaned.get('warranty_card_photo'):
                self.add_error('warranty_card_photo', 'Добавьте фото гарантийного талона.')
            if not str(cleaned.get('defect') or '').strip():
                self.add_error('defect', 'Коротко опишите неисправность.')
        return cleaned

