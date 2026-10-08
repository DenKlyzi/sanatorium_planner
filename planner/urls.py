from django.urls import path

from . import views

urlpatterns = [
    path('', views.search, name='search'),
    path('plan/', views.build_plan, name='build_plan'),
]
