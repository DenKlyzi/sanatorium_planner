from django import template

from planner.presentation import clean_copy as clean_route_copy
from planner.presentation import format_price_range
from planner.presentation import split_phrases as split_service_phrases

register = template.Library()


@register.filter
def group_by_institution(items):
    """Группирует площадки по учреждению, не меняя исходный порядок."""
    groups = []
    index = {}
    for item in items:
        site = _site(item)
        institution = site.institution
        key = institution.pk
        if key not in index:
            group = {'institution': institution, 'items': []}
            index[key] = group
            groups.append(group)
        index[key]['items'].append(item)
    return groups


def _site(item):
    nested = getattr(item, 'site', None)
    if nested is not None and getattr(nested, 'institution_id', None) is not None:
        return nested
    return item


@register.filter
def split_phrases(value):
    return split_service_phrases(value)


@register.filter
def price_range(site):
    return format_price_range(site.min_daily_price, site.max_daily_price)


@register.filter
def clean_copy(value):
    return clean_route_copy(value)
