from django.urls import path, register_converter

from oh_routes import ALIASES, ROUTES

from . import telemetry, views
from .addresses import RoomConverter
from .streams import events
from .topics import VIEWS

register_converter(RoomConverter, "room")

urlpatterns = [
    path("", views.home),
    *[path(route.lstrip("/"), views.asset, {"name": name}) for route, name in ROUTES.items() if route != "/"],
    *[path(route.lstrip("/"), views.page_alias, {"target": target}) for route, target in ALIASES.items()],
    path("api/v1/session", views.session), path("api/v1/logout", views.signout), path("api/v1/me", views.me),
    path("api/v1/security", views.security),
    path("api/v1/security/guests", views.guest_text),
    path("api/v1/token-login", views.token_login),
    path("api/v1/all-off", views.all_off),
    path("api/v1/telemetry", telemetry.report),
    path("api/v1/tokens", views.tokens), path("api/v1/tokens/<uuid:ident>", views.tokens),
    path("api/v1/users", views.users), path("api/v1/users/<int:ident>", views.users),
    path("api/v1/locks", views.locks),
    path("api/v1/history/rooms", views.room_history),
    path("api/v1/rooms/<room:room>/policy", views.room_policy),
    path("api/v1/rooms/<room:room>/lock", views.room_lock),
    path("api/v1/rooms/<room:room>/commands", views.command),
    path("rest/items", views.pairing_items), path("rest/items/<slug:item>", views.item_resource),
    path("rest/items/<slug:item>/state", views.item_state),
    # One snapshot-first event stream per page, by view (topics.py VIEWS).
    *[path("api/v1/events/" + view, events, {"view": view}) for view in VIEWS],
    path("unifi/media/<path:resource>", views.media),
    path("static/remote/", views.asset, {"name": "index.html"}),
    path("static/remote/<str:name>", views.asset),
]
