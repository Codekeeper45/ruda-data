from __future__ import annotations

import json

from starlette.requests import Request

from app.main import mutation_response, redirect


def test_redirect_places_query_before_profile_anchor() -> None:
    response = redirect("/profiles/review#profile-7", "Голос принят")
    location = response.headers["location"]
    assert location.startswith("/profiles/review?message=")
    assert location.endswith("#profile-7")


def test_ajax_mutation_returns_json_instead_of_redirect() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/samples/1/review",
            "headers": [(b"x-requested-with", b"XMLHttpRequest")],
        }
    )
    response = mutation_response(
        request,
        "/profiles/review#profile-1",
        "Голос принят",
        payload={"profile": {"id": 1}},
    )
    assert response.status_code == 200
    assert json.loads(response.body) == {
        "ok": True,
        "message": "Голос принят",
        "profile": {"id": 1},
    }
