"""WHEP (WebRTC-HTTP Egress Protocol) exposure for a single camera.

This is the HTTP door onto
:py:meth:`SpiriCamera.camera.Camera.webrtc_offer` and
:py:meth:`~SpiriCamera.camera.Camera.webrtc_close`, the same RPCs a
viewer uses over zenoh, so a viewer that came in over HTTP and one that
came in over SpiriSynq get the same video track and tags data channel,
from the camera's one encode.  Given a mirror, it only signals: the
camera's own process answers and sends the video.

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

from SpiriCamera.camera import Camera

#: Media type both the offer and the answer are carried in, per WHEP.
SDP_MEDIA_TYPE = "application/sdp"

#: Seconds to wait for the camera's answer, which waits up to 5 for ICE.
_NEGOTIATE_TIMEOUT = 15.0


def create_whep_app(get_camera: Callable[[], Camera], path: str = "/whep") -> FastAPI:
    """Build a WHEP app publishing whatever ``get_camera()`` returns.

    Parameters
    ----------
    get_camera : Callable[[], Camera]
        Called on every request, so the camera it returns can be
        swapped or reconfigured out from under a long-lived app.
    path : str
        Where the endpoint is, ``/whep`` unless a client expects another
        (Spiri Cloud's Frigate pulls ``/camera/whep``).

    Returns
    -------
    FastAPI
        An app with ``POST <path>`` and ``DELETE <path>/{session_id}``.
    """
    path = "/" + path.strip("/")
    app = FastAPI(title="SpiriCamera WHEP")

    # WHEP clients may ask first, for CORS or the server's ICE servers (we
    # have none to offer). Some, like MediaMTX's, give up if this fails.
    @app.options(path, status_code=204)
    async def whep_options() -> Response:
        return Response(status_code=204, headers={"Allow": "OPTIONS, POST"})

    @app.post(path, status_code=201)
    async def whep_offer(request: Request) -> Response:
        offer_sdp = (await request.body()).decode("utf-8")
        if not offer_sdp:
            raise HTTPException(400, "Request body must be an SDP offer")

        # Through the camera's own RPC: a mirror's reaches the camera's
        # process, which does the camera's one encode.
        answer = await (
            get_camera().webrtc_offer.timeout(_NEGOTIATE_TIMEOUT).as_async(offer_sdp, "offer")
        )
        return Response(
            content=answer["sdp"],
            media_type=SDP_MEDIA_TYPE,
            status_code=201,
            headers={"Location": f"{path}/{answer['session_id']}"},
        )

    @app.delete(path + "/{session_id}", status_code=204)
    async def whep_terminate(session_id: str) -> Response:
        closed = await get_camera().webrtc_close.as_async(session_id)
        if not closed:
            raise HTTPException(404, "No such WHEP session")
        return Response(status_code=204)

    return app


__all__ = ["SDP_MEDIA_TYPE", "create_whep_app"]
