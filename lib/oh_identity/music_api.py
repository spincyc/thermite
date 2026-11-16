"""The music endpoints (oh_identity.music; lib/oh_identity/README.md, Music).

Anyone who reads the site reads what the speakers play and commands them,
as a room's remote does; TIDAL's sign-in is an Admin's. A Guest who may not
see free text sees the speakers without titles or art, and is asked to sign
in for search, browse, queues and art, which are all free text.
"""

from django.http import HttpResponse, JsonResponse

from .auth import api, not_allowed, principal, problem_response
from .music import ART, ART_SIZES, DEFAULT_PAGE, OFFSET, PAGE, music_for
from .runtime import get_adapter
from .services import Problem


def see_text(request):
    from .security import guests_see_text
    return not getattr(request.identity_user, "is_guest", False) or guests_see_text(request.identity_state)


def require_text(request):
    if not see_text(request):
        raise Problem("login_required", 401)


def query(request, allowed):
    """The request's query parameters, each at most once and each allowed."""
    if set(request.GET) - set(allowed) or any(len(request.GET.getlist(key)) > 1 for key in request.GET):
        raise Problem("invalid_query")
    return {key: request.GET.get(key) for key in allowed}


def page(values):
    """(offset, limit) of a paged read."""
    offset, limit = values["offset"] or "0", values["limit"] or str(DEFAULT_PAGE)
    if not (offset.isascii() and offset.isdigit() and limit.isascii() and limit.isdigit()
            and int(offset) <= OFFSET and 1 <= int(limit) <= PAGE):
        raise Problem("invalid_query")
    return int(offset), int(limit)


def command_body(request, allowed, required=()):
    from .views import body
    data = body(request)
    if set(data) - set(allowed) or set(required) - set(data):
        raise Problem("invalid_command")
    return data


def accepted(answer):
    return JsonResponse(answer, status=202)


@api()
def overview(request):
    """The speakers, what each plays and the sources thermite has."""
    query(request, ())
    return JsonResponse(music_for(get_adapter()).read(see_text(request)))


@api()
def search(request):
    values = query(request, ("q", "kind", "source"))
    require_text(request)
    return JsonResponse(music_for(get_adapter()).search(values["q"] or "", values["kind"] or "", values["source"] or ""))


@api()
def browse(request):
    values = query(request, ("ref", "offset", "limit"))
    require_text(request)
    music = music_for(get_adapter())
    return JsonResponse(music.browse(values["ref"], *page(values)))


@api()
def item(request):
    """An album's, artist's or playlist's page (oh_identity.music.Music.details)."""
    values = query(request, ("ref",))
    require_text(request)
    return JsonResponse(music_for(get_adapter()).details(values["ref"]))


@api()
def queue(request, speaker):
    values = query(request, ("offset", "limit"))
    require_text(request)
    return JsonResponse(music_for(get_adapter()).queue(speaker, *page(values)))


@api(methods=("POST",))
def play(request, speaker):
    from .trail import origin
    data = command_body(request, ("ref", "option", "start", "shuffle"), ("ref",))
    return accepted(music_for(get_adapter()).play(request.identity_user, speaker, data["ref"], data.get("option", "replace"),
                                                  start=data.get("start"), shuffle=data.get("shuffle"), origin=origin(request)))


@api(methods=("POST",))
def transport(request, speaker):
    from .trail import origin
    data = command_body(request, ("action", "value"), ("action",))
    return accepted(music_for(get_adapter()).transport(request.identity_user, speaker, data["action"],
                                                       data.get("value"), origin=origin(request)))


@api(methods=("POST",))
def volume(request, speaker):
    from .trail import origin
    data = command_body(request, ("level", "group_level"))
    return accepted(music_for(get_adapter()).volume(request.identity_user, speaker, data, origin=origin(request)))


@api(methods=("POST",))
def group(request, speaker):
    """Group speakers under this one; joined and refused are read back, so
    the answer is the outcome, not an acceptance, unless no read-back
    answered: then the members asked for are pending."""
    from .trail import origin
    data = command_body(request, ("members",), ("members",))
    return JsonResponse(music_for(get_adapter()).group(request.identity_user, speaker, data["members"],
                                                       origin=origin(request)))


@api(methods=("POST",))
def ungroup(request, speaker):
    from .trail import origin
    command_body(request, ())
    return accepted(music_for(get_adapter()).ungroup(request.identity_user, speaker, origin=origin(request)))


@api()
def art(request, image):
    """One cover image at one of ART_SIZES, as Music Assistant's image proxy
    serves it: a bounded JPEG or PNG, cached privately for a day (an image's
    ID names its source, so it never changes)."""
    values = query(request, ("size",))
    if not ART.fullmatch(image) or (values["size"] or "256") not in ART_SIZES:
        raise Problem("not_found", 404)
    require_text(request)
    kind, content = music_for(get_adapter()).image(image, values["size"] or "256")
    response = HttpResponse(content, content_type=kind)
    response["Cache-Control"] = "private, max-age=86400"
    return response


@api(admin=True)
def tidal(request):
    query(request, ())
    return JsonResponse(music_for(get_adapter()).tidal_status())


@api(methods=("POST",), admin=True)
def tidal_start(request):
    command_body(request, ())
    return JsonResponse(music_for(get_adapter()).tidal_start())


@api(methods=("POST",), admin=True)
def tidal_finish(request):
    command_body(request, ())
    return JsonResponse(music_for(get_adapter()).tidal_finish())


def events(request):
    """The music event stream (oh_identity.music_hub)."""
    if request.method != "GET":
        return not_allowed("GET")
    try:
        if request.GET:
            raise Problem("not_found", 404)
        user = principal(request)
        adapter = get_adapter()
        music = music_for(adapter)
        from . import music_hub, streams
        return streams.respond(request, user, adapter, music_hub.VIEW, lambda joined: len(music.spec["speakers"]),
                               hub=lambda: music_hub.hub_for(music))
    except Problem as exc:
        return problem_response(exc)
