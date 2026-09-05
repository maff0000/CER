"""R7-CONTENT-TYPE: the transport ``Content-Type`` header is populated
automatically by every HTTP client, often to a form-encoding default
(``application/x-www-form-urlencoded``, ``multipart/form-data``) that
describes how the request was framed, never what the artifact bytes
actually are. ``POST /v1/artifacts`` takes the raw body verbatim and never
parses form encoding, so such a value can never be a truthful declaration
of an artifact's content type -- it must be treated as "not declared" and
recorded as the existing honest default (``application/octet-stream``)
instead of burned verbatim into a permanent, immutable evidence record
(PID Artifact doctrine: "preserve content/type metadata").

A genuine declaration -- anything else, from either the transport header
or the optional ``X-CER-Content-Type`` override -- must still be recorded
exactly as given, parameters and case intact.
"""

from __future__ import annotations

from .helpers import headers


def _register(client, *, payload: bytes = b"artifact bytes", filename: str = "a.bin", **extra):
    return client.post(
        "/v1/artifacts",
        content=payload,
        headers=headers(**{"X-CER-Filename": filename, **extra}),
    )


def _recorded_content_type(client, resp) -> str:
    assert resp.status_code == 201, resp.text
    artifact_id = resp.json()["artifact_id"]
    get_resp = client.get(f"/v1/artifacts/{artifact_id}", headers=headers())
    assert get_resp.status_code == 200, get_resp.text
    return get_resp.json()["content_type"]


# --- genuine declarations survive verbatim --------------------------------


def test_declared_text_csv_is_recorded_verbatim(client):
    resp = _register(client, **{"Content-Type": "text/csv"})
    assert _recorded_content_type(client, resp) == "text/csv"


def test_declared_application_json_is_recorded_verbatim(client):
    resp = _register(client, **{"Content-Type": "application/json"})
    assert _recorded_content_type(client, resp) == "application/json"


def test_declared_image_png_is_recorded_verbatim(client):
    resp = _register(client, **{"Content-Type": "image/png"})
    assert _recorded_content_type(client, resp) == "image/png"


def test_declared_type_with_parameters_survives_intact(client):
    resp = _register(client, **{"Content-Type": "text/csv; charset=utf-8"})
    assert _recorded_content_type(client, resp) == "text/csv; charset=utf-8"


# --- transport form-encoding defaults are NOT declarations ----------------


def test_form_urlencoded_default_records_octet_stream(client):
    resp = _register(client, **{"Content-Type": "application/x-www-form-urlencoded"})
    assert _recorded_content_type(client, resp) == "application/octet-stream"


def test_form_urlencoded_with_parameters_records_octet_stream(client):
    resp = _register(
        client, **{"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
    )
    assert _recorded_content_type(client, resp) == "application/octet-stream"


def test_form_urlencoded_mixed_case_records_octet_stream(client):
    resp = _register(client, **{"Content-Type": "Application/X-WWW-Form-Urlencoded"})
    assert _recorded_content_type(client, resp) == "application/octet-stream"


def test_multipart_form_data_with_boundary_records_octet_stream(client):
    resp = _register(
        client,
        **{"Content-Type": "multipart/form-data; boundary=----WebKitFormBoundary7MA4YWx"},
    )
    assert _recorded_content_type(client, resp) == "application/octet-stream"


def test_missing_content_type_records_octet_stream(client):
    # No "Content-Type" key at all -- the pre-existing "producer told us
    # nothing" case, which must keep behaving exactly as before.
    resp = _register(client)
    assert _recorded_content_type(client, resp) == "application/octet-stream"


# --- X-CER-Content-Type: optional explicit-declaration override ----------


def test_x_cer_content_type_takes_precedence_over_form_default(client):
    resp = _register(
        client,
        **{
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CER-Content-Type": "text/csv",
        },
    )
    assert _recorded_content_type(client, resp) == "text/csv"


def test_x_cer_content_type_takes_precedence_over_a_genuine_transport_value(client):
    resp = _register(
        client,
        **{
            "Content-Type": "application/json",
            "X-CER-Content-Type": "application/vnd.cer.custom+json",
        },
    )
    assert _recorded_content_type(client, resp) == "application/vnd.cer.custom+json"


def test_omitting_x_cer_content_type_changes_nothing(client):
    # Same request as test_declared_text_csv_is_recorded_verbatim, minus
    # the override header -- proves the new header is additive, not a
    # behaviour change for producers who never send it.
    resp = _register(client, **{"Content-Type": "text/csv"})
    assert _recorded_content_type(client, resp) == "text/csv"


def test_x_cer_content_type_itself_form_encoded_falls_through_to_transport(client):
    # An X-CER-Content-Type value that is itself a form-encoding default
    # is not a genuine declaration either -- falls through to the
    # transport header, then to the default, by the same rule.
    resp = _register(
        client,
        **{
            "Content-Type": "text/csv",
            "X-CER-Content-Type": "multipart/form-data",
        },
    )
    assert _recorded_content_type(client, resp) == "text/csv"
