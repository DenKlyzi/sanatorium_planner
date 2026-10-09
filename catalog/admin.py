from django.contrib import admin

from catalog.models import Institution, Site


class SiteInline(admin.StackedInline):
    model = Site
    extra = 0
    show_change_link = True
    fields = (
        'address',
        'latitude',
        'longitude',
        'season',
        'limited_mobility_access',
        'rating',
        'transport_accessibility',
        'min_daily_price',
        'max_daily_price',
        'procedures',
        'excursions',
    )


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ('name', 'treatment_profile')
    search_fields = ('name', 'treatment_profile', 'description')
    inlines = (SiteInline,)


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = (
        'institution',
        'address',
        'season',
        'limited_mobility_access',
        'rating',
        'transport_accessibility',
        'min_daily_price',
        'max_daily_price',
    )
    list_filter = (
        'season',
        'limited_mobility_access',
        'rating',
        'transport_accessibility',
        'institution',
    )
    search_fields = ('address', 'procedures', 'excursions', 'institution__name')
    autocomplete_fields = ('institution',)
