from django.contrib import admin

from matching.models import SiteEmbedding


@admin.register(SiteEmbedding)
class SiteEmbeddingAdmin(admin.ModelAdmin):
    list_display = ('site', 'model_name', 'text_hash')
    list_filter = ('model_name',)
    search_fields = (
        'text_hash',
        'model_name',
        'site__address',
        'site__institution__name',
    )
    autocomplete_fields = ('site',)
