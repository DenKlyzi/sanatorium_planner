"""Форма подбора, карточки площадок и сохранение плана."""

from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from catalog.models import Site
from matching import llm_client
from matching.engine import Preferences, explain, match

from .forms import PlanBuildForm, SearchForm
from .models import Plan


PLAN_SYSTEM_PROMPT = (
    'Ты тур-агент, составляющий маршрут по санаториям и площадкам Калужской области. '
    'Напиши пошаговый текст маршрута на русском языке с учётом бюджета и пожеланий пользователя. '
    'Для каждой площадки укажи: примерный день визита, как туда добраться, '
    'стоимость и ключевые процедуры и экскурсии. В конце дай общий бюджетный итог и советы. '
    'Структурированный текст с заголовками, 150–400 слов. Без эмодзи.'
)

_MATCH_ERRORS = (OSError, ValueError, RuntimeError, ImportError)
_LLM_ERRORS = (OSError, ValueError, RuntimeError)


@dataclass(frozen=True)
class SiteCard:
    site: Site
    explanation: str


@login_required
@require_http_methods(['GET', 'POST'])
def search(request: HttpRequest) -> HttpResponse:
    if request.method == 'GET':
        return render(request, 'planner/search.html', {'form': SearchForm()})

    form = SearchForm(request.POST)
    if not form.is_valid():
        return render(request, 'planner/search.html', {'form': form})

    preferences = _preferences(form.cleaned_data)
    try:
        scored = match(preferences)
    except _MATCH_ERRORS as exc:
        detail = str(exc).strip() if str(exc).strip() else 'Неизвестная ошибка.'
        return render(
            request,
            'planner/search.html',
            {
                'form': form,
                'match_error': f'Не удалось подобрать площадки. {detail}',
            },
            status=503,
        )

    explain_error = None
    cards = []
    for item in scored:
        explanation = ''
        if explain_error is None:
            try:
                explanation = explain(preferences, item.site)
            except _LLM_ERRORS as exc:
                detail = str(exc).strip() if str(exc).strip() else 'неизвестная ошибка'
                explain_error = f'Не удалось получить объяснение. {detail}.'
        cards.append(SiteCard(site=item.site, explanation=explanation))

    return render(
        request,
        'planner/result.html',
        {
            'form': form,
            'cards': cards,
            'plan_form': _plan_form(form.cleaned_data),
            'explain_error': explain_error,
        },
    )


@login_required
@require_POST
def build_plan(request: HttpRequest) -> HttpResponse:
    form = PlanBuildForm(request.POST)
    if not form.is_valid():
        return render(request, 'planner/plan_error.html', {'form': form}, status=400)

    sites = _ordered_sites(form.cleaned_data['sites'], request.POST.getlist('sites'))
    try:
        route_text = llm_client.complete(
            PLAN_SYSTEM_PROMPT,
            _route_user_text(form.cleaned_data, sites),
        ).strip()
    except _LLM_ERRORS as exc:
        detail = str(exc).strip() if str(exc).strip() else 'неизвестная ошибка'
        return render(
            request,
            'planner/plan_error.html',
            {'error': f'Не удалось собрать маршрут. {detail}.'},
            status=502,
        )
    if not route_text:
        return render(
            request,
            'planner/plan_error.html',
            {'error': 'Модель вернула пустой маршрут.'},
            status=502,
        )

    with transaction.atomic():
        plan = Plan.objects.create(
            user=request.user,
            route_text=route_text,
        )
        plan.sites.set(sites)

    return redirect('plan_detail', pk=plan.pk)


@login_required
def plan_list(request: HttpRequest) -> HttpResponse:
    """Список показывает только планы текущего пользователя."""
    plans = (
        Plan.objects
        .filter(user=request.user)
        .prefetch_related('sites__institution')
        .order_by('-id')
    )
    return render(request, 'planner/plan_list.html', {'plans': plans})


@login_required
def plan_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Чужой план по прямой ссылке недоступен."""
    plan = get_object_or_404(Plan, pk=pk, user=request.user)
    sites = plan.sites.select_related('institution').all()
    return render(request, 'planner/plan.html', {
        'plan': plan,
        'sites': sites,
    })


@login_required
@require_POST
def plan_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Удаление удаляет только свой план."""
    plan = get_object_or_404(Plan, pk=pk, user=request.user)
    plan.delete()
    messages.success(request, 'План удалён.')
    return redirect('plan_list')


def _preferences(cleaned) -> Preferences:
    parts = []
    if cleaned['region']:
        parts.append(f'Регион: {cleaned["region"]}')
    if cleaned['budget'] is not None:
        parts.append(f'Бюджет за сутки: {cleaned["budget"]}')
    if cleaned['query']:
        parts.append(cleaned['query'])
    if cleaned['procedures']:
        parts.append(f'Желаемые процедуры: {cleaned["procedures"]}')
    if cleaned['excursions']:
        parts.append(f'Желаемые экскурсии: {cleaned["excursions"]}')
    return Preferences(
        budget=cleaned['budget'],
        region=cleaned['region'],
        query='\n'.join(parts),
    )


def _plan_form(cleaned) -> PlanBuildForm:
    return PlanBuildForm(
        auto_id='plan_%s',
        initial={
            'budget': cleaned['budget'],
            'region': cleaned['region'],
            'query': cleaned['query'],
            'procedures': cleaned['procedures'],
            'excursions': cleaned['excursions'],
        },
    )


def _ordered_sites(chosen, posted_ids) -> list[Site]:
    by_id = {site.pk: site for site in chosen}
    ordered = []
    seen = set()
    for raw in posted_ids:
        try:
            pk = int(raw)
        except (TypeError, ValueError):
            continue
        site = by_id.get(pk)
        if site is None or pk in seen:
            continue
        seen.add(pk)
        ordered.append(site)
    for site in chosen:
        if site.pk not in seen:
            ordered.append(site)
    return ordered


def _route_user_text(cleaned, sites: list[Site]) -> str:
    lines = ['Составь текст маршрута по выбранным площадкам.']

    if cleaned['region']:
        lines.append(f'Регион: {cleaned["region"]}')
    if cleaned['budget'] is not None:
        lines.append(f'Бюджет за сутки: {cleaned["budget"]}')
    if cleaned['query']:
        lines.append(f'Пожелания: {cleaned["query"]}')
    if cleaned['procedures']:
        lines.append(f'Желаемые процедуры: {cleaned["procedures"]}')
    if cleaned['excursions']:
        lines.append(f'Желаемые экскурсии: {cleaned["excursions"]}')

    lines.append('')
    lines.append('Площадки (в порядке посещения):')
    for i, site in enumerate(sites, 1):
        lines.append(f'{i}. {site.institution.name}')
        if getattr(site, 'address', None):
            lines.append(f'   Адрес: {site.address}')
        if site.min_daily_price is not None and site.max_daily_price is not None:
            lines.append(f'   Цена за сутки: {site.min_daily_price}–{site.max_daily_price} ₽')
        elif site.min_daily_price is not None:
            lines.append(f'   Цена за сутки: от {site.min_daily_price} ₽')
        if getattr(site, 'transport_accessibility', None) is not None:
            lines.append(f'   Транспортная доступность: {site.transport_accessibility} из 5')
        if getattr(site, 'procedures', None):
            lines.append(f'   Процедуры: {site.procedures}')
        if getattr(site, 'excursions', None):
            lines.append(f'   Экскурсии: {site.excursions}')

    lines.append('')
    lines.append('Требования к тексту:')
    lines.append('- Язык: русский. Без эмодзи.')
    lines.append('- Без ** и ## пиши.')
    lines.append('- Порядок площадок — строго как в списке выше.')
    lines.append('- Цены и сроки бери только из переданных данных, не придумывай.')
    lines.append('- Для каждой площадки: день визита, как добраться, стоимость, ключевые процедуры и экскурсии.')
    lines.append('- В конце — общий бюджетный итог и 2–3 совета.')

    return '\n'.join(lines)