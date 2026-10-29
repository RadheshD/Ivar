from django.urls import path

from . import views

app_name = "whatsapp"

urlpatterns = [
    path("", views.webhook, name="webhook"),
    path("order-events/", views.order_status_events, name="order-events"),
]
