from django.urls import include, path

urlpatterns = [
    path("health/", include("host.urls")),
    path("webhooks/whatsapp/", include("apps.whatsapp.urls")),
]

handler404 = "host.views.not_found"
handler500 = "host.views.server_error"
