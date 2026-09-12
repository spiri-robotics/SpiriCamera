"""WHEP (WebRTC-HTTP Egress Protocol) exposure for a single camera.

This is the HTTP door onto the same session machinery
:py:meth:`SpiriCamera.camera.Camera.webrtc_offer` uses over zenoh --
both call straight into :py:mod:`SpiriCamera.webrtc`, so a viewer that
came in over HTTP and one that came in over SpiriSynq get the same
video track and tags data channel.

Deliberately minimal: one camera per app (:py:func:`create_whep_app`
takes a callable returning it, matching the "expose *a* topic over
http" ask rather than a multi-camera router), and no ``PATCH`` route
for trickle ICE -- :py:mod:`SpiriCamera.webrtc` waits for ICE gathering
to finish before answering, so there is nothing left to trickle. A
future need for faster connection setup through restrictive NATs is
the reason to revisit that, not anything this module is missing today.

Run standalone with ``SpiriCamera whep-serve <source>`` (see
:py:mod:`SpiriCamera.cli`), or mount ``create_whep_app(...)`` into any
other FastAPI app.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request, Response

from SpiriCamera import webrtc
from SpiriCamera.camera import Camera

#: Media type both the offer and the answer are carried in, per WHEP.
SDP_MEDIA_TYPE = "application/sdp"


def create_whep_app(get_camera: Callable[[], Camera]) -> FastAPI:
    """Build a WHEP app publishing whatever ``get_camera()`` returns.

    Parameters
    ----------
    get_camera : Callable[[], Camera]
        Called on every request, so the camera it returns can be
        swapped or reconfigured out from under a long-lived app.

    Returns
    -------
    FastAPI
        An app with ``POST /whep`` and ``DELETE /whep/{session_id}``.
    """
    app = FastAPI(title="SpiriCamera WHEP")

    @app.post("/whep", status_code=201)
    async def whep_offer(request: Request) -> Response:
        offer_sdp = (await request.body()).decode("utf-8")
        if not offer_sdp:
            raise HTTPException(400, "Request body must be an SDP offer")

        session_id, answer_sdp, answer_type = await webrtc.negotiate_async(
            get_camera(), offer_sdp, "offer"
        )
        return Response(
            content=answer_sdp,
            media_type=SDP_MEDIA_TYPE,
            status_code=201,
            headers={"Location": f"/whep/{session_id}"},
        )

    @app.delete("/whep/{session_id}", status_code=204)
    async def whep_terminate(session_id: str) -> Response:
        closed = await webrtc.close_session_async(session_id)
        if not closed:
            raise HTTPException(404, "No such WHEP session")
        return Response(status_code=204)

    return app


__all__ = ["create_whep_app", "SDP_MEDIA_TYPE"]
