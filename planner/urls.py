from django.urls import path
from . import views

urlpatterns = [
    path('', views.search, name='search'),
    path('plan/', views.build_plan, name='build_plan'),
    path('plans/', views.plan_list, name='plan_list'),
    path('plans/<int:pk>/', views.plan_detail, name='plan_detail'),
    path('plans/<int:pk>/delete/', views.plan_delete, name='plan_delete'),
]