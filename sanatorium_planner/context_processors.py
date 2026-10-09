from django.conf import settings


def yandex_maps(request):
    return {'YANDEX_MAPS_API_KEY': settings.YANDEX_MAPS_API_KEY}
