from django import forms
from django.contrib.auth.forms import AuthenticationForm

from catalog.models import Site


_REGION_MAX = 255
_TEXT_MAX = 5000


class BootstrapAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class SearchForm(forms.Form):
    budget = forms.DecimalField(
        label='Бюджет за сутки',
        help_text='Максимальная цена за сутки.',
        min_value=0,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={'class': 'form-control', 'min': '0', 'step': '0.01'},
        ),
    )
    region = forms.CharField(
        label='Регион',
        required=False,
        max_length=_REGION_MAX,
        help_text='Ищется в адресе площадки.',
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )
    query = forms.CharField(
        label='Свободный текст предпочтений',
        required=False,
        max_length=_TEXT_MAX,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
    )
    procedures = forms.CharField(
        label='Желаемые процедуры',
        required=False,
        max_length=_TEXT_MAX,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
    )
    excursions = forms.CharField(
        label='Желаемые экскурсии',
        required=False,
        max_length=_TEXT_MAX,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
    )


class PlanBuildForm(SearchForm):
    sites = forms.ModelMultipleChoiceField(
        label='Площадки',
        queryset=Site.objects.select_related('institution'),
        error_messages={'required': 'Выберите хотя бы одну площадку.'},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['budget'].widget = forms.HiddenInput()
        for name in ('region', 'query', 'procedures', 'excursions'):
            self.fields[name].widget = forms.Textarea(attrs={'hidden': True})
