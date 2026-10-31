from django.urls import path

from . import views
from .streams import events

urlpatterns = [
    path("api/v1/session", views.session), path("api/v1/logout", views.signout), path("api/v1/me", views.me),
    path("api/v1/token-login", views.token_login),
    path("api/v1/all-off", views.all_off),
    path("api/v1/tokens", views.tokens), path("api/v1/tokens/<uuid:ident>", views.tokens),
    path("api/v1/users", views.users), path("api/v1/users/<int:ident>", views.users),
    path("api/v1/roles", views.roles), path("api/v1/roles/<int:ident>", views.roles),
    path("api/v1/rooms/<slug:room>/policy", views.room_policy),
    path("api/v1/rooms/<slug:room>/commands", views.command),
    path("rest/items", views.pairing_items), path("rest/items/<slug:item>", views.item_resource),
    path("rest/items/<slug:item>/state", views.item_state), path("rest/events", events),
    path("unifi/media/<path:resource>", views.media),
    path("static/remote/", views.asset, {"name": "index.html"}),
    path("static/remote/<str:name>", views.asset),
]
