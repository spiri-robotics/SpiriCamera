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
SpiriCamera.transports
SpiriCamera.video
SpiriCamera.rtsp
SpiriCamera.klv
SpiriCamera.exif
SpiriCamera.overlay
SpiriCamera.projection
SpiriCamera.sources
SpiriCamera.ui
SpiriCamera.web
SpiriCamera.cli
SpiriCamera.main
```

- {doc}`SpiriCamera.camera` — capture lifecycle, encoding, and publication
- {doc}`SpiriCamera.transports` — the transport objects and their wire formats
- {doc}`SpiriCamera.video` — the camera's one H.264 encode and its rate control
- {doc}`SpiriCamera.rtsp` — the RTSP server
- {doc}`SpiriCamera.klv` — KLV metadata for each frame
- {doc}`SpiriCamera.exif` — frame metadata carried inside the encoded frame
- {doc}`SpiriCamera.overlay` — SVG HUD overlays baked into frames before encoding
- {doc}`SpiriCamera.projection` — where a point in the world lands in a camera's picture
- {doc}`SpiriCamera.sources` — source handler registry and resolution
- {doc}`SpiriCamera.ui` — NiceGUI debug UI
- {doc}`SpiriCamera.web` — an unused stub; the debug UI is `SpiriCamera.ui`
- {doc}`SpiriCamera.cli` — command-line interface
- {doc}`SpiriCamera.main` — main entry point
