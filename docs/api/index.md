(api-reference)=
# API Reference

The SpiriCamera package provides camera functionality for video capture and frame processing, built into the SpiriSynq ecosystem.

## Classes

```{eval-rst}
.. automodule:: SpiriCamera
   :members:
   :member-order: bysource
   :undoc-members: False
   :show-inheritance:
   :noindex:
```

## Modules

```{toctree}
:hidden:

SpiriCamera.camera
SpiriCamera.exif
SpiriCamera.overlay
SpiriCamera.sources
SpiriCamera.webrtc
SpiriCamera.whep
SpiriCamera.ui
SpiriCamera.web
SpiriCamera.cli
SpiriCamera.main
```

- {doc}`SpiriCamera.camera` — capture lifecycle, encoding, and publication
- {doc}`SpiriCamera.exif` — frame metadata carried inside the encoded frame
- {doc}`SpiriCamera.overlay` — SVG HUD overlays baked into frames before encoding
- {doc}`SpiriCamera.sources` — source handler registry and resolution
- {doc}`SpiriCamera.webrtc` — WebRTC plumbing shared by every signaling path
- {doc}`SpiriCamera.whep` — WHEP (WebRTC-HTTP Egress Protocol) exposure for a single camera
- {doc}`SpiriCamera.ui` — NiceGUI debug UI
- {doc}`SpiriCamera.web` — web UI entry point (optional SpiriConfig plugin)
- {doc}`SpiriCamera.cli` — command-line interface
- {doc}`SpiriCamera.main` — main entry point
