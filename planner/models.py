from django.conf import settings
from django.db import models

from catalog.models import Site


class Plan(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='plans',
        verbose_name='пользователь',
    )
    sites = models.ManyToManyField(
        Site,
        related_name='plans',
        verbose_name='площадки',
    )
    route_text = models.TextField('текст маршрута')

    class Meta:
        verbose_name = 'план'
        verbose_name_plural = 'планы'
        ordering = ['-id']

    def __str__(self):
        return f'План {self.pk}'