from django.contrib import admin

from catalog.models import Institution, Site


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ('name', 'treatment_profile')
    search_fields = ('name', 'treatment_profile', 'description')


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = (
        'institution',
        'address',
        'transport_accessibility',
        'min_daily_price',
        'max_daily_price',
    )
    list_filter = ('transport_accessibility', 'institution')
    search_fields = ('address', 'procedures', 'excursions', 'institution__name')
    autocomplete_fields = ('institution',)
