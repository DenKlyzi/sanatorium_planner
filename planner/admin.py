from django.contrib import admin

from .models import Plan


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ('pk', 'user')
    search_fields = ('route_text', 'user__username')
    filter_horizontal = ('sites',)
