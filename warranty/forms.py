from django import forms

from warranty.models import WarrantyClaim


class WarrantyClaimUpdateForm(forms.ModelForm):
    class Meta:
        model = WarrantyClaim
        fields = ('status', 'priority', 'assigned_to', 'due_at', 'comment')
        widgets = {
            'status': forms.Select(attrs={'class': 'form-select'}),
            'priority': forms.Select(attrs={'class': 'form-select'}),
            'assigned_to': forms.Select(attrs={'class': 'form-select'}),
            'due_at': forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}),
            'comment': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
        }


class WarrantyWorkItemForm(forms.Form):
    kind = forms.ChoiceField(label='Тип', choices=(), widget=forms.Select(attrs={'class': 'form-select'}))
    name = forms.CharField(label='Работа или запчасть', max_length=500, widget=forms.TextInput(attrs={'class': 'form-control'}))
    notes = forms.CharField(label='Примечание', required=False, widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from warranty.models import WarrantyWorkItem
        self.fields['kind'].choices = WarrantyWorkItem.Kind.choices
