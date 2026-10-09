from django.db import models


class SiteEmbedding(models.Model):
    """Вектор площадки для пары «текст + модель эмбеддингов».

    Старые хеши не удаляются: если текст вернули назад, повторный подбор
    берёт сохранённый вектор и не вызывает модель.
    """

    site = models.ForeignKey(
        'catalog.Site',
        on_delete=models.CASCADE,
        related_name='embeddings',
        verbose_name='площадка',
    )
    text_hash = models.CharField('хеш текста', max_length=64)
    model_name = models.CharField('имя модели', max_length=255)
    vector = models.JSONField('вектор')

    class Meta:
        verbose_name = 'эмбеддинг площадки'
        verbose_name_plural = 'эмбеддинги площадок'
        ordering = ['site_id', 'model_name', 'text_hash']
        constraints = [
            models.UniqueConstraint(
                fields=['site', 'text_hash', 'model_name'],
                name='site_embedding_site_hash_model_uniq',
            ),
        ]

    def __str__(self):
        return f'{self.site_id} {self.model_name} {self.text_hash[:12]}'
