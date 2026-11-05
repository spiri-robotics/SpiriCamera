"""SVG HUD overlays, baked into frames before they are encoded.

An overlay is a :py:class:`HudWidget`: a small SVG template, anchored to a
corner or a percentage coordinate of the frame. Widgets are not owned by
the camera that draws them -- each is its own :py:class:`SyncableObject`,
published wherever its author runs (a UI panel, an ML detector, ...), and
:py:class:`OverlayMixin` only mirrors the ones a camera's
``overlay_widgets`` names.

A widget's SVG is templated with MiniJinja against three namespaces:
``exif`` (the current frame's tags, see :py:mod:`SpiriCamera.exif`),
``frame`` (the current frame's actual, as-received width/height/framerate
-- see :py:attr:`FRAME_NAME`), and
``objects`` (the latest field values of whatever other SpiriSynq objects
the widget declares for itself). ``frame`` exists alongside ``objects``
rather than folded into it because these values -- what the *rendered*
frame actually is, this call -- are never published as synced fields at
all (``CameraBase.synq_skip_sync``, see :py:func:`camera_metrics_widget`);
there is no topic a widget could declare to reach them, only the local
values :py:meth:`OverlayMixin._render_overlays` already has in hand for
the frame it is about to composite onto.

A real topic path is never written directly into a ``{{ }}`` expression
-- only an alias is, as ``{{ objects.<alias>.<field> }}``, because a
zenoh topic can contain characters (a hyphen in a hostname, say) that are
not valid there. The alias, and the *default* object it resolves to, are
declared right next to where the alias is used, as a MiniJinja comment
MiniJinja itself already strips at render time and that never appears in
the rendered SVG::

    {# object: battery = azrael/battery_monitor #}
    <text>{{ objects.battery.voltage }}</text>

A declared object is a whole :py:class:`~SpiriSynq.syncable_objects.SyncableObject`
-- every one of its synced fields is available as ``objects.<alias>.<field>``,
mirrored generically via
:py:meth:`~SpiriSynq.session.Session.from_topic_untyped` (so the widget
author needs no locally-importable class for it, and never sees its RPC
methods -- ``from_topic_untyped`` only ever exposes synced attributes).
This is templated in :py:func:`resolve_objects`, which reads both halves
of that same text -- MiniJinja's own static analysis
(``undeclared_variables_in_str``) for what the render body actually
reads, against :py:func:`declared_objects` for what has been declared --
and raises if a template reads an alias with no matching declaration,
which is meant to be caught by whoever is authoring the widget, not
silently ignored.

The declared topic is only a *default*. Whichever camera is actually
rendering the widget can override it -- to point ``battery`` at a
different concrete object than the widget author had in mind -- via
:py:attr:`OverlayMixin.overlay_widgets`, without touching the shared
:py:class:`HudWidget` itself (which would repoint it for every other
camera mirroring the same widget too). The same field also carries a
per-camera position override, for exactly the same reason: a widget's own
``anchor``/``custom_anchor_x``/``custom_anchor_y`` are its default
placement, and a camera can place it elsewhere for itself alone. See
:py:attr:`OverlayMixin.overlay_widgets` for the shape of both.

A widget can also be placed by where its objects are in the world rather
than on the frame: ``{# anchor: <alias> #}`` centres it on an object's
pixel, ``{# anchor: each <alias>.<field> #}`` draws a copy on each object
that field links to, and ``{# project: <alias>, ... #}`` gives a
frame-wide widget its objects' pixels. Those widgets get a ``view``
namespace (and ``item``, for ``each``), computed by
:py:mod:`SpiriCamera.projection` from the camera's own pose and lens; see
:py:func:`world_placement` and :ref:`world-overlays`.

Rendered SVG is rasterized with ``thorvg_python`` at its own **natural**
pixel size (``Picture.get_size()``), never scaled to fit a declared box:
a widget author who writes ``font-size="24"`` gets a real 24px on screen
regardless of the camera's resolution. Scaling a small raster up to fit a
4K frame (or down to fit a low-res one) would either blur the text or
make it illegible -- crisp, fixed-size text was chosen over staying a
constant fraction of the frame across resolutions.

The one deliberate exception is a root ``<svg>`` whose ``width`` and
``height`` are *both* percentages (``width="20%" height="20%"``): that
scales the whole widget, preserving its own aspect ratio, to fit within
that percentage of the frame being rendered onto -- see
:py:func:`rasterize`. This is opt-in, not a default: a widget author who
wants "always a fifth of the picture, whatever that picture's
resolution" now has a way to say so, alongside the natural-pixel-size
behaviour above, which stays the default for a plain ``width="260"``.
Which is right depends on the widget -- fixed-size text and a HUD panel
generally want to stay crisp and constant-sized; a logo, icon, or other
graphic that is meant to track the frame's proportions wants percentage
sizing instead.
"""

from __future__ import annotations

import functools
import importlib.resources
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import minijinja
import numpy as np
import thorvg_python as thorvg
from loguru import logger
from psygnal.containers import EventedDict
from SpiriSynq.example_types.position import Pose
from SpiriSynq.syncable_objects import SyncableObject

from SpiriCamera.projection import View, project

if TYPE_CHECKING:
    # Deferred: SpiriCamera.camera imports this module, not the other
    # way around -- only the type hint on camera_metrics_widget needs it.
    from SpiriCamera.camera import Camera

#: Fractional (x, y) of the *frame* that a named anchor's widget corner
#: should land on. ``0.5`` centres the widget over that axis; ``1.0``
#: flushes the widget's far edge against the frame's far edge.
_NAMED_ANCHORS: dict[str, tuple[float, float]] = {
    "top_left": (0.0, 0.0),
    "top_middle": (0.5, 0.0),
    "top_right": (1.0, 0.0),
    "center_left": (0.0, 0.5),
    "center_middle": (0.5, 0.5),
    "center_right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0),
    "bottom_middle": (0.5, 1.0),
    "bottom_right": (1.0, 1.0),
}

#: Every valid :py:attr:`HudWidget.anchor` value, named anchors plus
#: ``"custom"`` -- for a UI to offer as a choice, since ``_NAMED_ANCHORS``
#: alone would leave "custom" out.
ANCHORS: tuple[str, ...] = (*_NAMED_ANCHORS, "custom")

#: Top-level template name a widget's declared, live SpiriSynq objects are
#: exposed under, e.g. ``{{ objects.battery.voltage }}`` for the alias
#: ``battery``.
OBJECTS_NAME = "objects"

#: Top-level template name the current frame's EXIF tags are exposed under.
EXIF_NAME = "exif"

#: Top-level template name the current frame's actual, as-received
#: ``width``/``height``/``framerate`` are exposed under, e.g.
#: ``{{ frame.width }}x{{ frame.height }}``. See the module docstring for
#: why these are injected directly rather than reached through ``objects``.
FRAME_NAME = "frame"

#: Top-level template name for where the widget's objects are in the
#: picture, for a widget that declares an ``anchor`` or ``project``; see
#: :py:func:`world_placement` and :ref:`world-overlays`.
VIEW_NAME = "view"

#: Top-level template name for the one linked object an
#: ``{# anchor: each <alias>.<field> #}`` widget is being drawn for, its
#: fields as ``{{ item.<field> }}``.
ITEM_NAME = "item"

#: Matches an object declaration comment: `{# object: alias = real/topic #}`.
#: A dedicated, narrow syntax we own completely -- not an attempt to
#: parse arbitrary MiniJinja expression syntax -- which is what keeps it
#: reliable: the literal words `object:` and `=` are fixed, the alias is
#: a plain identifier, and everything from `=` up to `#}` is the topic,
#: whitespace-trimmed.
_OBJECT_DECLARATION_RE = re.compile(
    r"\{#\s*object:\s*(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<topic>[^#]+?)\s*#\}"
)

#: Matches an anchor declaration: `{# anchor: alias #}`, or
#: `{# anchor: each alias.field #}`. Same narrow-syntax reasoning as
#: `_OBJECT_DECLARATION_RE`.
_ANCHOR_DECLARATION_RE = re.compile(
    r"\{#\s*anchor:\s*(?:(?P<each>each)\s+)?"
    r"(?P<alias>[A-Za-z_][A-Za-z0-9_]*)(?:\.(?P<field>[A-Za-z_][A-Za-z0-9_]*))?\s*#\}"
)

#: Matches a projection declaration: `{# project: alias, other #}`.
_PROJECT_DECLARATION_RE = re.compile(
    r"\{#\s*project:\s*(?P<aliases>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*#\}"
)

#: How long to wait before trying again to mirror a widget or object
#: topic that did not resolve. Mirroring runs on the capture thread, and
#: a failed attempt costs SpiriSynq's retries, so a topic that does not
#: exist -- half-typed, or a widget deleted while still listed -- must
#: not be retried every frame.
_OVERLAY_RETRY_INTERVAL = 2.0

_env = minijinja.Environment()
# Default ("chainable") undefined behavior renders a bare missing value as
# an empty string rather than raising -- fine for `{{ objects.a.b }}` where
# `a` is entirely absent (accessing `.b` on it does raise), but a plain
# `{{ objects.<alias>.<field> }}` with no value yet would then silently
# render as "" instead of the per-frame render failure this module relies
# on to skip a widget until its alias has resolved to something.
_env.undefined_behavior = "strict"

#: One process-wide software-rendering engine. thorvg initializes itself
#: once; a second engine per widget would be pure overhead.
_engine = thorvg.Engine()
_engine.init(threads=0)

#: The name every bundled-font ``<text font-family="...">`` in this
#: module's own templates uses, and the only font guaranteed to be
#: available regardless of the host -- see the loading block below.
#: Not an arbitrary label: ``font_load()`` registers a font under the
#: family name baked into the file's own metadata, so this has to match
#: Miracode's actual name table entry, verified empirically below.
DEFAULT_FONT = "Miracode"

#: Bundled rather than discovered from the host's installed fonts:
#: thorvg does not fall back to any OS text rendering -- an SVG
#: `<text>` element with no matching loaded font simply draws nothing,
#: on every platform, including the slim Docker image this project
#: ships (which has no fonts installed at all). A widget author who
#: forgets to reference a loaded font by name gets a silently blank
#: widget, which is exactly the failure this avoids for SpiriCamera's
#: own templates.
#:
#: Loaded from a real file path (`font_load`), not `font_load_data`
#: from in-memory bytes -- this build of thorvg returns
#: `Result.NOT_SUPPORTED` for `font_load_data` regardless of the
#: mimetype string given it, but the file-path loader works. Simpler to
#: resolve the packaged asset's real on-disk path directly than to
#: route around that through `importlib.resources.as_file()`'s
#: context-manager lifetime for what is a one-time, process-lifetime
#: registration anyway.
#:
#: Miracode was picked for legibility at the small sizes an overlay
#: renders at, not for completeness -- it is missing the Ukrainian
#: letters U+0490/U+0491 (Ge with upturn). A widget that needs those
#: specifically should load and reference its own font instead of
#: relying on this default; see `thorvg_python.paint.text.Text.font_load`.
#:
#: font_load() is a global registration despite living on a `Text`
#: instance -- the instance itself is discarded immediately, only used
#: to reach the underlying (process-wide, thorvg-internal) font cache.
#: Public so a client-render path (:py:meth:`OverlayMixin.render_overlays_for_client`)
#: can embed the same file as a browser ``@font-face``, keeping the
#: browser's rendering of ``DEFAULT_FONT`` visually identical to thorvg's
#: rather than falling back to whatever generic font the browser picks
#: for an unresolvable ``font-family``.
DEFAULT_FONT_PATH = importlib.resources.files("SpiriCamera").joinpath("assets/Miracode.ttf")
_font_result = thorvg.Text(_engine).font_load(str(DEFAULT_FONT_PATH))
if _font_result != thorvg.Result.SUCCESS:
    logger.warning(f"overlay: could not load bundled font {DEFAULT_FONT!r}: {_font_result}")

# Three more thorvg SVG-parser gaps, same family as the font-loading one
# above, all affecting how `<text>` renders:
#
# 1. `dy` on `<text>` is silently ignored -- a template that writes
#    `<text y="20" dy="-4">` renders identically to one with no `dy` at
#    all, baseline pinned to `y`. A widget author positioning text
#    relative to some other element (a box's top edge, say) has to bake
#    that offset into `y` itself, e.g. `y="{{ box.y - 4 }}"` rather than
#    `y="{{ box.y }}" dy="-4"` -- see ``examples/yolo_overlay.py``'s
#    ``BOX_OVERLAY_SVG`` for a worked example. There is no general fix
#    for this one short of rewriting every `dy` into the `y` it modifies,
#    which is indistinguishable from just not using `dy`.
# 1b. `stroke`, `stroke-width`, and `paint-order` are all silently
#    ignored on `<text>` too -- there is no way to get a real
#    outlined/stroked label out of thorvg. `fill` on `<text>` works
#    fine, so a widget author wanting a label legible over an arbitrary
#    background fakes the outline instead: draw the same text several
#    times at small pixel offsets in one colour (typically black), then
#    once more, undisplaced, in the real colour on top -- see
#    ``examples/yolo_overlay.py``'s ``BOX_OVERLAY_SVG`` (``halo_offsets``)
#    for a worked example.
# 2. Independent of (1): even a plain `y` with no `dy` involved lands a
#    few pixels low of where the same SVG renders in a browser -- see
#    :py:func:`_correct_text_baseline` and ``_TEXT_BASELINE_SLOPE``/
#    ``_TEXT_BASELINE_INTERCEPT`` below for the empirical fix
#    :py:func:`rasterize` applies for this one automatically.
#
# Neither affects a browser, which honors `dy` and places a plain `y`
# baseline correctly -- see :py:meth:`OverlayMixin.render_overlays_for_client`,
# which renders the *un*corrected template text client-side and would
# double-correct if it reused either workaround.


class OverlayError(ValueError):
    """Raised when a widget's SVG cannot be templated or rasterized."""


@dataclass
class HudWidget(SyncableObject):
    """One overlay element: an SVG template anchored on the frame.

    Owned by whoever authors the overlay, not by the camera that draws
    it -- a camera only mirrors widgets named in its ``overlay_widgets``
    (see :py:class:`OverlayMixin`), it never creates or publishes one.

    Attributes
    ----------
    svg_template : str
        MiniJinja source for the widget's SVG. May reference
        ``{{ exif.* }}`` for the current frame's tags, and
        ``{{ objects.<alias>.<field> }}`` for any other SpiriSynq
        object's latest field values, provided the same alias is
        declared with a ``{# object: <alias> = <default topic> #}``
        comment somewhere in this same template; see
        :py:func:`resolve_objects`. The declared topic is only a
        default -- a camera rendering this widget can bind the alias to
        a different object of its own choosing; see
        :py:attr:`OverlayMixin.overlay_widgets`.
    anchor : str
        This widget's *default* placement: one of the nine-point grid
        (``top_left`` .. ``bottom_right``) or ``"custom"`` to use
        :py:attr:`custom_anchor_x`/:py:attr:`custom_anchor_y` instead.
        A camera rendering this widget can override the placement for
        itself alone; see :py:attr:`OverlayMixin.overlay_widgets`.
    custom_anchor_x : float
        Percent (0-100) of the frame's width. Only used when
        :py:attr:`anchor` is ``"custom"``, in which case it names the
        widget's top-left corner directly -- unlike the named anchors,
        which use the widget's own rendered size to flush it against an
        edge or centre it.
    custom_anchor_y : float
        Percent (0-100) of the frame's height. See
        :py:attr:`custom_anchor_x`.
    z_index : float
        Paint order relative to this camera's other overlay widgets --
        higher paints on top, lower underneath. Widgets sharing a value
        keep the stable order they'd otherwise render in (insertion
        order of :py:attr:`OverlayMixin.overlay_widgets`). Ties with
        another widget's overlapping frame content (e.g. a bounding-box
        widget meant to sit under a HUD) are the usual reason to set
        this away from the default.
    """

    svg_template: str = ""
    anchor: str = "top_left"
    custom_anchor_x: float = 0.0
    custom_anchor_y: float = 0.0
    z_index: float = 0.0


def declared_objects(svg_template: str) -> dict[str, str]:
    """Alias -> default SpiriSynq topic declarations in a widget's own
    template.

    A widget declares every object it needs right where it uses it, as a
    comment MiniJinja already strips at render time::

        {# object: battery = azrael/battery_monitor #}

    Parameters
    ----------
    svg_template : str
        A :py:attr:`HudWidget.svg_template`.

    Returns
    -------
    dict[str, str]
        ``{alias: default_topic}``, one entry per declaration found. A
        template with none declares nothing.
    """
    return {
        match["alias"]: match["topic"] for match in _OBJECT_DECLARATION_RE.finditer(svg_template)
    }


def resolve_objects(svg_template: str) -> dict[str, str]:
    """Aliases a widget's template actually reads, checked against its
    own declarations.

    Cross-checks two things read out of the same template text: every
    ``{{ objects.<alias>.<field> }}`` MiniJinja's own static analysis
    (``undeclared_variables_in_str``) finds the render body actually
    using, against every alias :py:func:`declared_objects` finds
    declared. This is the enforcement point the module docstring
    describes -- a widget author gets a clear, immediate error for a
    typo'd or missing declaration, rather than a silently-undefined
    value only visible in the rendered picture.

    Parameters
    ----------
    svg_template : str
        A :py:attr:`HudWidget.svg_template`.

    Returns
    -------
    dict[str, str]
        ``{alias: default_topic}``, restricted to aliases the template
        body actually reads -- a declared-but-unused alias is not
        mirrored.

    Raises
    ------
    OverlayError
        If the template is malformed -- as it is part way through an
        edit -- or reads ``objects.<alias>`` for an alias with no
        matching declaration.
    """
    prefix = f"{OBJECTS_NAME}."
    try:
        names = _env.undeclared_variables_in_str(svg_template, nested=True)
    except Exception as exc:
        raise OverlayError(f"could not parse overlay template: {exc}") from exc
    used: set[str] = set()
    for name in names:
        if not name.startswith(prefix):
            continue
        # Only the segment right after `objects.` is the alias --
        # `objects.battery.voltage` reads field `voltage` off alias
        # `battery`, and that deeper access is the expected shape now
        # that an alias names a whole object, not one scalar value.
        alias = name.removeprefix(prefix).split(".", 1)[0]
        if alias:
            used.add(alias)

    # An object a widget is placed on, or projects, is one it uses, even
    # if its template never reads a field of it.
    used |= set(world_placement(svg_template).aliases)

    declared = declared_objects(svg_template)
    missing = sorted(used - declared.keys())
    if missing:
        raise OverlayError(
            f"template reads objects.{missing[0]} with no matching "
            f"'{{# object: {missing[0]} = ... #}}' declaration"
        )

    return {alias: declared[alias] for alias in used}


@dataclass(frozen=True)
class WorldPlacement:
    """How a widget is drawn by where its objects are in the world.

    What :py:func:`world_placement` read from a template's ``anchor`` and
    ``project`` declarations; see :ref:`world-overlays`.

    Attributes
    ----------
    anchor : str | None
        The alias the widget is centred on, ``{# anchor: <alias> #}``,
        or whose linked objects it is drawn on, one copy each, with
        :py:attr:`each`.  ``None`` for a widget placed on the frame.
    each : str | None
        The field of :py:attr:`anchor`'s object holding the links,
        ``{# anchor: each <alias>.<field> #}``.
    project : tuple[str, ...]
        Aliases whose pixels the widget is given, as
        ``{{ view.<alias> }}``, ``{# project: <alias>, ... #}``.
    """

    anchor: str | None = None
    each: str | None = None
    project: tuple[str, ...] = ()

    @property
    def aliases(self) -> tuple[str, ...]:
        """Every alias this placement needs mirrored."""
        return ((self.anchor,) if self.anchor else ()) + self.project


def world_placement(svg_template: str) -> WorldPlacement:
    """A widget's ``anchor`` and ``project`` declarations.

    Parameters
    ----------
    svg_template : str
        A :py:attr:`HudWidget.svg_template`.

    Returns
    -------
    WorldPlacement
        Empty for a widget that declares neither, the usual case.

    Raises
    ------
    OverlayError
        For more than one anchor, an anchor and a projection in one
        widget, ``each`` without a field (or a field without ``each``),
        or an alias with no ``{# object: #}`` declaration.
    """
    anchors = list(_ANCHOR_DECLARATION_RE.finditer(svg_template))
    project = tuple(
        alias.strip()
        for match in _PROJECT_DECLARATION_RE.finditer(svg_template)
        for alias in match["aliases"].split(",")
    )
    if len(anchors) > 1:
        raise OverlayError("a widget has one anchor, this one declares several")
    if anchors and project:
        raise OverlayError("a widget is either anchored on an object or projects objects, not both")
    placement = WorldPlacement(project=project)
    if anchors:
        match = anchors[0]
        if bool(match["each"]) != bool(match["field"]):
            raise OverlayError(
                f"'{match[0]}': anchor on one object as 'anchor: <alias>', "
                "or on each of its links as 'anchor: each <alias>.<field>'"
            )
        placement = WorldPlacement(anchor=match["alias"], each=match["field"])
    declared = declared_objects(svg_template)
    for alias in placement.aliases:
        if alias not in declared:
            raise OverlayError(
                f"template places itself by {alias!r} with no matching "
                f"'{{# object: {alias} = ... #}}' declaration"
            )
    return placement


def render_svg(
    svg_template: str,
    *,
    objects: Mapping[str, Mapping[str, object]],
    exif_tags: Mapping[str, str],
    frame_info: Mapping[str, object],
    view: Mapping[str, object] | None = None,
    item: Mapping[str, object] | None = None,
) -> str:
    """Render a widget's template against live object field values, EXIF
    tags, and the actual frame being rendered onto.

    Parameters
    ----------
    svg_template : str
        A :py:attr:`HudWidget.svg_template`.
    objects : Mapping[str, Mapping[str, object]]
        ``{alias: {field: value}}`` for the aliases :py:func:`resolve_objects`
        found in use, exposed as ``{{ objects.<alias>.<field> }}``. An
        alias the template reads but that is missing here renders as
        undefined, which MiniJinja raises on -- see ``Raises`` below.
    exif_tags : Mapping[str, str]
        The current frame's tags, exposed as ``{{ exif.* }}``.
    frame_info : Mapping[str, object]
        The current frame's actual ``width``/``height``/``framerate``,
        exposed as ``{{ frame.* }}``. See :py:attr:`FRAME_NAME` and the
        module docstring for why these are injected directly instead of
        being reached through ``objects``.
    view : Mapping[str, object], optional
        Where the widget's objects are in the picture, exposed as
        ``{{ view.* }}``: the anchor's :py:class:`~SpiriCamera.projection.View`
        fields, or ``{alias: fields or None}`` for a projecting widget.
        Only for a widget with a :py:func:`world_placement`.
    item : Mapping[str, object], optional
        The linked object an ``anchor: each`` copy is drawn for, its
        fields exposed as ``{{ item.* }}``.

    Returns
    -------
    str
        The rendered SVG source.

    Raises
    ------
    OverlayError
        If the template is malformed, or reads an alias with no value
        yet (e.g. an object that has not resolved, or a field it does
        not have). Either way this is one frame's problem, not a reason
        to stop rendering the widget forever -- the caller is expected
        to retry on the next frame.
    """
    try:
        return _env.render_str(
            svg_template,
            **{
                OBJECTS_NAME: {alias: dict(fields) for alias, fields in objects.items()},
                EXIF_NAME: dict(exif_tags),
                FRAME_NAME: dict(frame_info),
                **({VIEW_NAME: dict(view)} if view is not None else {}),
                **({ITEM_NAME: dict(item)} if item is not None else {}),
            },
        )
    except Exception as exc:
        raise OverlayError(f"could not render overlay template: {exc}") from exc


#: Matches a root ``<svg ...>`` opening tag.
_ROOT_SVG_TAG_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)

#: Matches a ``width="20%"``/``height="20%"`` attribute within a tag
#: matched by ``_ROOT_SVG_TAG_RE``.
_PERCENT_SIZE_ATTR_RE = re.compile(r'\b(width|height)\s*=\s*"(-?[0-9]+(?:\.[0-9]+)?)%"')

#: Matches a ``<text ...>`` opening tag, to read/rewrite its ``y`` for
#: :py:func:`_correct_text_baseline`. Deliberately does not also match
#: ``<tspan>`` -- none of this module's own templates or
#: ``examples/yolo_overlay.py`` use one, and a tspan's ``y`` is relative
#: to its parent ``<text>`` in a way this line-level regex has no way to
#: track.
_TEXT_TAG_RE = re.compile(r"<text\b[^>]*>", re.IGNORECASE)

#: Matches a ``y="..."`` attribute within a tag matched by ``_TEXT_TAG_RE``.
_Y_ATTR_RE = re.compile(r'\by\s*=\s*"(-?[0-9]+(?:\.[0-9]+)?)"')

#: Matches a ``font-size="..."`` attribute within a tag matched by
#: ``_TEXT_TAG_RE``.
_FONT_SIZE_ATTR_RE = re.compile(r'\bfont-size\s*=\s*"(-?[0-9]+(?:\.[0-9]+)?)"')

#: ``font-size`` this module's default font renders at when a ``<text>``
#: does not declare one of its own -- the CSS/SVG initial value, same as
#: every browser assumes.
_DEFAULT_TEXT_FONT_SIZE = 16.0

#: Empirical fit (least-squares over ``font-size`` 8..64, error <=1px in
#: that range) for how many pixels low of its declared ``y`` thorvg's
#: software rasterizer actually draws a ``<text>`` baseline, using the
#: bundled Miracode font -- see :py:func:`_correct_text_baseline`. Not
#: derived from any documented thorvg behaviour (there is none to derive
#: it from); re-measure both constants (render "H" at a few font sizes,
#: as in the exploration this fix came from, and compare the solid
#: pixel rows against the declared ``y``) if thorvg or the bundled font
#: is ever upgraded and overlay text visibly drifts again.
_TEXT_BASELINE_SLOPE = 0.1915
_TEXT_BASELINE_INTERCEPT = -1.074


def _correct_text_baseline(svg: str) -> str:
    """Shift every ``<text>``'s ``y`` up to counter thorvg's low baseline.

    Only meant for the SVG about to be handed to thorvg
    (:py:func:`rasterize`) -- a browser (:py:meth:`OverlayMixin.render_overlays_for_client`)
    already places a plain ``y`` correctly and must render the
    *uncorrected* template, or it would shift text that was never wrong
    there in the first place. See the module-level comment above
    ``_TEXT_BASELINE_SLOPE`` for where the correction itself comes from.

    A ``<text>`` with no ``y`` at all is left alone -- there is nothing
    here to shift.
    """

    def _shift(match: re.Match[str]) -> str:
        tag = match.group(0)
        y_match = _Y_ATTR_RE.search(tag)
        if y_match is None:
            return tag
        font_size_match = _FONT_SIZE_ATTR_RE.search(tag)
        font_size = float(font_size_match.group(1)) if font_size_match else _DEFAULT_TEXT_FONT_SIZE
        correction = round(_TEXT_BASELINE_SLOPE * font_size + _TEXT_BASELINE_INTERCEPT)
        if correction <= 0:
            return tag
        corrected_y = float(y_match.group(1)) - correction
        return tag[: y_match.start(1)] + f"{corrected_y:g}" + tag[y_match.end(1) :]

    return _TEXT_TAG_RE.sub(_shift, svg)


def _percent_size(svg: str) -> tuple[float, float] | None:
    """``(width%, height%)`` if the root ``<svg>`` declares both as
    percentages, else ``None``.

    All-or-nothing: a ``width`` given as a percentage with an absolute
    ``height`` (or vice versa) does not count -- there is no meaning for
    "20% wide, 50px tall" in :py:func:`rasterize`.
    """
    match = _ROOT_SVG_TAG_RE.search(svg)
    if match is None:
        return None
    attrs = dict(_PERCENT_SIZE_ATTR_RE.findall(match.group(0)))
    if "width" not in attrs or "height" not in attrs:
        return None
    return float(attrs["width"]), float(attrs["height"])


def _strip_percent_size(svg: str) -> str:
    """Drop percentage ``width``/``height`` from the root ``<svg>`` tag.

    Leaves thorvg to fall back to the SVG's own ``viewBox`` for its
    natural size -- see :py:func:`rasterize`, which scales that natural
    size against the *frame* itself, rather than let thorvg resolve the
    percentage against the ``viewBox`` (a different, and here irrelevant,
    reference size).
    """

    def _strip(match: re.Match[str]) -> str:
        return _PERCENT_SIZE_ATTR_RE.sub("", match.group(0))

    return _ROOT_SVG_TAG_RE.sub(_strip, svg, count=1)


def _load_and_measure(
    svg: str,
    frame_width: int | None,
    frame_height: int | None,
) -> tuple[thorvg.Picture, float, float]:
    """Parse ``svg`` into a thorvg ``Picture`` and resolve its pixel
    size, without drawing it.

    Shared by :py:func:`rasterize` (which draws the returned picture)
    and :py:func:`measure_size` (which only needs the size) -- see
    :py:func:`rasterize` for the percentage-sizing behavior this
    implements.

    Raises
    ------
    OverlayError
        If the SVG could not be parsed, declares no size, or uses
        percentage sizing with no frame size given to scale it against.
    """
    percent_size = _percent_size(svg)
    if percent_size is not None:
        if frame_width is None or frame_height is None:
            raise OverlayError(
                "overlay SVG has a percentage width/height but no frame "
                "size was given to scale it against"
            )
        svg = _strip_percent_size(svg)

    # thorvg objects are native and never freed by Python's garbage
    # collector: whoever holds the returned picture must hand it to a
    # canvas (which then owns it) or release it with unref(True).
    picture = thorvg.Picture(_engine)
    result = picture.load_data(svg.encode("utf-8"), mimetype="svg", rpath=None, copy=True)
    if result != 0:
        picture.unref(True)
        raise OverlayError(f"thorvg could not parse overlay SVG (result {result})")

    _, natural_width, natural_height = picture.get_size()
    if natural_width <= 0 or natural_height <= 0:
        picture.unref(True)
        raise OverlayError(f"overlay SVG has no natural size ({natural_width}x{natural_height})")

    if percent_size is not None:
        width_pct, height_pct = percent_size
        box_width = width_pct / 100.0 * frame_width
        box_height = height_pct / 100.0 * frame_height
        # The smaller of the two ratios, applied uniformly to both axes,
        # so the widget fits *within* the percentage box without being
        # stretched out of its own proportions.
        scale = min(box_width / natural_width, box_height / natural_height)
        natural_width *= scale
        natural_height *= scale
        picture.set_size(natural_width, natural_height)

    return picture, natural_width, natural_height


def measure_size(
    svg: str,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> tuple[int, int]:
    """The pixel size :py:func:`rasterize` would draw ``svg`` at, without
    actually rasterizing it.

    Used by the client-render path (:py:meth:`OverlayMixin.render_overlays_for_client`)
    to keep a browser-laid-out widget's box pixel-identical to the one
    :py:func:`anchor_position` assumes server-side, without paying for a
    software rasterization pass just to learn a size.

    Parameters, return value, and exceptions: see :py:func:`rasterize`.
    """
    picture, natural_width, natural_height = _load_and_measure(svg, frame_width, frame_height)
    picture.unref(True)
    width, height = math.ceil(natural_width), math.ceil(natural_height)
    if width <= 0 or height <= 0:
        raise OverlayError(f"overlay SVG has no natural size ({width}x{height})")
    return width, height


def _normalize_svg_size(svg: str, width: float, height: float) -> str:
    """Overwrite the root ``<svg>``'s ``width``/``height`` (percentage or
    absolute, whatever it currently declares) with explicit pixel values.

    Used by the client-render path so the ``<svg>`` element a browser
    actually lays out occupies exactly the box :py:func:`measure_size`
    (and, before it, :py:func:`anchor_position`) computed -- otherwise a
    percentage-sized widget would be laid out at its full declared box
    and its content merely letterboxed inside it, drifting off the
    anchor position computed for the smaller, aspect-fit box.
    """

    def _replace(match: re.Match[str]) -> str:
        tag = _PERCENT_SIZE_ATTR_RE.sub("", match.group(0))
        tag = re.sub(r'\s(width|height)\s*=\s*"[^"]*"', "", tag)
        return tag[:-1] + f' width="{width}" height="{height}">'

    return _ROOT_SVG_TAG_RE.sub(_replace, svg, count=1)


def rasterize(
    svg: str,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> np.ndarray:
    """Parse and rasterize an SVG, at its own natural pixel size or
    scaled to a percentage of the frame.

    Never forces a size for an ordinarily-sized widget: its declared
    ``width``/``height`` or ``viewBox`` *is* the pixel size it is
    rendered and, later, composited at, unscaled. See the module
    docstring for why, and for the one exception this function does
    implement: a root ``<svg>`` whose ``width`` and ``height`` are both
    percentages is scaled -- preserving its own aspect ratio -- to fit
    within that percentage of ``frame_width``/``frame_height``, the same
    way CSS ``object-fit: contain`` would.

    Parameters
    ----------
    svg : str
        Rendered SVG source, as returned by :py:func:`render_svg`.
    frame_width, frame_height : int, optional
        Size of the frame this is about to be composited onto. Only
        consulted -- and required -- when ``svg``'s root ``width``/
        ``height`` are both percentages; ignored otherwise.

    Returns
    -------
    np.ndarray
        An ``(height, width, 4)`` ``uint8`` array, straight (not
        premultiplied) RGBA, transparent where nothing was drawn.

    Raises
    ------
    OverlayError
        If the SVG could not be parsed, declares no size to render at,
        or uses percentage sizing with no frame size given to scale it
        against.
    """
    svg = _correct_text_baseline(svg)
    picture, natural_width, natural_height = _load_and_measure(svg, frame_width, frame_height)

    width, height = math.ceil(natural_width), math.ceil(natural_height)
    if width <= 0 or height <= 0:
        picture.unref(True)
        raise OverlayError(f"overlay SVG has no natural size ({width}x{height})")

    canvas = thorvg.SwCanvas(_engine)
    try:
        canvas.set_target(width, height)
        if canvas.add(picture) != thorvg.Result.SUCCESS:
            picture.unref(True)
        canvas.draw(True)
        canvas.sync()

        buffer = np.ctypeslib.as_array(canvas.buffer_arr)
        # .copy(): buffer_arr's backing memory belongs to `canvas`, which is
        # destroyed right below.
        return buffer.view(np.uint8).reshape(height, width, 4).copy()
    finally:
        # Frees the canvas and the picture it now owns. thorvg_python has
        # no __del__, so without this every widget leaked its canvas and
        # picture on every frame -- gigabytes an hour on a HUD.
        canvas.destroy()


def anchor_position(
    anchor: str,
    custom_x: float,
    custom_y: float,
    frame_width: int,
    frame_height: int,
    widget_width: int,
    widget_height: int,
) -> tuple[int, int]:
    """Pixel position for a widget's top-left corner.

    Parameters
    ----------
    anchor : str
        A :py:attr:`HudWidget.anchor` value.
    custom_x, custom_y : float
        :py:attr:`HudWidget.custom_anchor_x`/``_y``, used only when
        ``anchor == "custom"``.
    frame_width, frame_height : int
        Size of the frame the widget is being composited onto.
    widget_width, widget_height : int
        Size of the rasterized widget, i.e. :py:func:`rasterize`'s
        output shape.

    Returns
    -------
    tuple[int, int]
        ``(x, y)`` of the widget's top-left corner, in frame pixels. Not
        clamped to the frame -- :py:func:`composite` clips it.
    """
    if anchor == "custom":
        return (
            round(custom_x / 100.0 * frame_width),
            round(custom_y / 100.0 * frame_height),
        )

    fraction_x, fraction_y = _NAMED_ANCHORS.get(anchor, _NAMED_ANCHORS["top_left"])
    return (
        round(fraction_x * (frame_width - widget_width)),
        round(fraction_y * (frame_height - widget_height)),
    )


def composite(frame: np.ndarray, rgba: np.ndarray, x: int, y: int) -> np.ndarray:
    """Alpha-blend a rasterized widget onto a copy of a BGR frame.

    Parameters
    ----------
    frame : np.ndarray
        A ``(height, width, 3)`` BGR frame, as read from a source.
    rgba : np.ndarray
        A widget raster from :py:func:`rasterize`.
    x, y : int
        Top-left corner to place ``rgba`` at, from
        :py:func:`anchor_position`. May be negative, or place ``rgba``
        partly or fully outside ``frame``; the overlap is clipped.

    Returns
    -------
    np.ndarray
        A new array -- ``frame`` is never mutated in place, so a camera
        with no overlays configured is unaffected by this module, and
        :py:meth:`~SpiriCamera.camera.Camera._encode`'s
        cache-by-identity check still sees a stable object when overlays
        happen to render byte-identical output.
    """
    result = frame.copy()
    _blend_into(result, *_blend_form(rgba), x, y)
    return result


def _blend_form(rgba: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """A widget raster as :py:func:`_blend_into` blends it: its colour
    already multiplied by its alpha, as BGR, and its inverse alpha, both
    ``uint16``. Worked out once per raster, not once per frame."""
    alpha = rgba[:, :, 3:4].astype(np.uint16)
    premultiplied = (rgba[:, :, 2::-1].astype(np.uint16) * alpha + 127) // 255
    inverse_alpha = 255 - alpha
    premultiplied.flags.writeable = False
    inverse_alpha.flags.writeable = False
    return premultiplied, inverse_alpha


@functools.lru_cache(maxsize=32)
def _rasterize_for_blend(
    svg: str, frame_width: int, frame_height: int
) -> tuple[np.ndarray, np.ndarray]:
    """:py:func:`rasterize`, in :py:func:`_blend_form`, cached by the
    rendered SVG: most widgets render the same SVG frame after frame (a
    status line, a battery, anything not moving), and parsing and drawing
    it again each frame was most of a HUD's CPU. Shared between callers,
    so read-only."""
    return _blend_form(rasterize(svg, frame_width, frame_height))


def _blend_into(
    frame: np.ndarray,
    premultiplied: np.ndarray,
    inverse_alpha: np.ndarray,
    x: int,
    y: int,
) -> None:
    """Alpha-blend a widget, in :py:func:`_blend_form`, onto ``frame`` in
    place, at ``(x, y)``, clipped to the frame -- see :py:func:`composite`.

    Integer maths throughout: ``v / 255``, rounded, is
    ``(v + 128 + ((v + 128) >> 8)) >> 8`` for any ``v`` a ``uint16``
    product of two bytes can be, which is what makes this cheaper than
    blending in floats.
    """
    frame_height, frame_width = frame.shape[:2]
    widget_height, widget_width = inverse_alpha.shape[:2]

    src_x, src_y = max(0, -x), max(0, -y)
    dst_x, dst_y = max(0, x), max(0, y)
    width = min(widget_width - src_x, frame_width - dst_x)
    height = min(widget_height - src_y, frame_height - dst_y)
    if width <= 0 or height <= 0:
        return

    source = (slice(src_y, src_y + height), slice(src_x, src_x + width))
    background = frame[dst_y : dst_y + height, dst_x : dst_x + width]
    blended = background * inverse_alpha[source]
    blended += 128
    blended += blended >> 8
    blended >>= 8
    blended += premultiplied[source]
    # Each half rounds on its own, so a nearly opaque pixel over a bright
    # background can come to 256.
    np.minimum(blended, 255, out=blended)
    background[...] = blended


# ---------------------------------------------------------------------------
# Ready-made widgets
# ---------------------------------------------------------------------------


def camera_metrics_widget(camera: Camera, *, anchor: str = "bottom_left") -> HudWidget:
    """Build and publish a widget showing a camera's own live stats.

    A ready-made overlay rather than a hand-authored one: baking a
    camera's actual resolution, framerate, and quality onto its own
    picture is common enough -- a debugging HUD, a sanity check that
    frames are really arriving at the rate they should -- to not be
    re-typed as SVG each time.

    ``width``/``height``/``framerate`` come from ``{{ frame.* }}``,
    injected directly by :py:meth:`OverlayMixin._render_overlays` rather
    than read as an object field: ``received_width``/``received_height``/
    ``received_framerate`` are deliberately never published as fields of
    their own (see ``CameraBase.synq_skip_sync``), so there is no live
    object this widget -- or any widget -- could declare to reach them;
    see the module docstring for why ``frame`` exists as its own
    namespace for exactly this. ``quality`` is read *live* off
    ``camera`` itself instead, exactly like any other overlay widget's
    ``objects.<alias>.<field>`` values -- ``camera`` is consulted only
    here, at build time, to learn its own topic, which becomes the
    ``cam`` alias's *default* binding (see the module docstring): no
    separate wiring step is needed, ``cam.overlay_widgets[widget_topic]
    = {}`` is enough to mirror this widget and have it resolve ``cam``
    to ``camera`` itself.

    The second line reads ``{{ exif.timestamp }}`` -- the frame's own
    capture time, unlike the render-time values above -- with a MiniJinja
    ``default`` filter so ``Camera.exif_enabled = False`` (which leaves
    ``exif_tags`` empty) renders ``"untagged"`` instead of failing the
    whole widget's render under this module's strict-undefined setting.

    Parameters
    ----------
    camera : SpiriCamera.camera.Camera
        The camera to describe.
    anchor : str
        Where to anchor the widget; see :py:attr:`HudWidget.anchor`.

    Returns
    -------
    HudWidget
        A published, authoritative widget under its own topic
        (``f"{camera.synq_topic}_metrics"``). The caller owns it, and
        should ``close()`` it when done -- typically alongside
        ``camera`` itself.
    """
    topic = camera.synq_absolute_path

    svg_template = f"""\
{{# object: cam = {topic} #}}
<svg xmlns="http://www.w3.org/2000/svg" width="260" height="52">
  <rect width="260" height="52" fill="black" fill-opacity="0.55"/>
  <text x="8" y="20" font-family="{DEFAULT_FONT}" font-size="16" fill="white"
      >{{{{ frame.width }}}}x{{{{ frame.height }}}} {{{{ frame.framerate }}}}fps q={{{{ objects.cam.quality }}}}</text>
  <text x="8" y="40" font-family="{DEFAULT_FONT}" font-size="16" fill="white"
      >{{{{ exif.timestamp | default("untagged") }}}}</text>
</svg>"""  # noqa: E501 - SVG, and its text lines are clearer whole

    return HudWidget(
        synq_topic=f"{camera.synq_topic}_metrics",
        synq_authoritive=True,
        anchor=anchor,
        svg_template=svg_template,
    )


#: The bundled Ghostscript tiger, as vendored -- a ``viewBox="0 0 900
#: 900"`` and no ``width``/``height`` of its own. :py:func:`tiger_widget`
#: adds a percentage ``width``/``height`` on top of this at build time,
#: so the fraction of the frame it fills is a parameter, not baked into
#: the file.
_tiger_svg_path = importlib.resources.files("SpiriCamera").joinpath("assets/tiger.svg")


def tiger_widget(
    camera: Camera, *, size_percent: float = 20.0, anchor: str = "center_middle"
) -> HudWidget:
    """Build and publish a widget showing the classic Ghostscript tiger.

    A ready-made demonstration of percentage sizing (see
    :py:func:`rasterize`): the vendored artwork is a fixed, unrelated
    900x900 ``viewBox``, but ``size_percent`` is baked into the widget's
    root ``<svg width="{size_percent}%" height="{size_percent}%">`` so it
    always renders at that fraction of the frame -- shrinking or growing
    with the camera's resolution -- rather than at some fixed pixel size
    that would look tiny on a 4K frame and oversized on a small one.

    Parameters
    ----------
    camera : SpiriCamera.camera.Camera
        The camera this widget's topic is namespaced under. The artwork
        itself does not depend on ``camera`` otherwise.
    size_percent : float
        Fraction of the frame, in each dimension, the tiger is scaled to
        fit within, preserving its own aspect ratio.
    anchor : str
        Where to anchor the widget; see :py:attr:`HudWidget.anchor`.

    Returns
    -------
    HudWidget
        A published, authoritative widget under its own topic
        (``f"{camera.synq_topic}_tiger"``). The caller owns it, and
        should ``close()`` it when done -- typically alongside
        ``camera`` itself.
    """
    svg = _tiger_svg_path.read_text(encoding="utf-8")
    svg_template = _ROOT_SVG_TAG_RE.sub(
        lambda match: match.group(0).replace(
            "<svg ", f'<svg width="{size_percent}%" height="{size_percent}%" ', 1
        ),
        svg,
        count=1,
    )

    return HudWidget(
        synq_topic=f"{camera.synq_topic}_tiger",
        synq_authoritive=True,
        anchor=anchor,
        svg_template=svg_template,
    )


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


@dataclass
class OverlayMixin:
    """Overlay support for a :py:class:`~SpiriCamera.camera.CameraBase`.

    The only synced field is :py:attr:`overlay_widgets`; every other
    piece of state this needs (mirrored widgets, mirrored data-source
    objects, per-widget error de-duplication, cached template-parse
    results) is plain instance state the host class sets up itself -- see
    :py:class:`~SpiriCamera.camera.Camera`'s constructor, which does the
    same for its own EXIF tag providers. This mixin has no
    ``__post_init__`` of its own and starts nothing by itself; the host
    class calls :py:meth:`_overlay_sync_widgets` once after construction
    (to pick up a rehydrated or constructor-supplied ``overlay_widgets``
    immediately) and :py:meth:`_render_overlays` from its own capture
    pipeline.
    """

    #: ``{widget_topic: usage}``, where ``widget_topic`` is a mirrored
    #: :py:class:`HudWidget`'s absolute topic and ``usage`` is a plain
    #: dict of *this camera's* overrides for that widget -- everything in
    #: it is optional:
    #:
    #: - ``"x"``, ``"y"``: percent-of-frame position, overriding the
    #:   widget's own ``anchor``/``custom_anchor_x``/``custom_anchor_y``
    #:   for this camera alone (same semantics as the widget's own
    #:   ``custom`` anchor). Omitted -> use the widget's own placement,
    #:   same as every other camera mirroring it.
    #: - ``"bindings"``: ``{alias: object_topic}``, overriding which
    #:   object a widget's declared alias resolves to, for this camera
    #:   alone. Omitted, or missing a given alias -> use that alias's own
    #:   ``{# object: alias = default_topic #}`` declaration.
    #: - ``"z_index"``: overrides the widget's own
    #:   :py:attr:`HudWidget.z_index` for this camera alone -- e.g. a
    #:   bounding-box widget authored to sit underneath, but wanted on
    #:   top for one particular camera. Omitted -> use the widget's own
    #:   ``z_index``, same as every other camera mirroring it.
    #:
    #: A widget's mere presence as a key is what makes this camera render
    #: it -- there is no separate list of topics. An
    #: :py:class:`~psygnal.containers.EventedDict`, not a plain ``dict``,
    #: so SpiriSynq republishes the whole mapping on any mutation (add,
    #: remove, or edit a usage dict) the same way it already does for any
    #: other evented container field -- see
    #: ``SyncableObject._zenoh_publish_changes``. Live: editing this at
    #: runtime adds, drops, rebinds, or repositions widgets on the next
    #: frame, no restart needed.
    overlay_widgets: EventedDict = field(default_factory=EventedDict)

    #: When set, :py:meth:`~SpiriCamera.camera.Camera.read` skips
    #: :py:meth:`_render_overlays` entirely -- the raw frame is encoded
    #: unmodified, and a consumer (e.g. the NiceGUI UI) is expected to
    #: call :py:meth:`render_overlays_for_client` itself and render the
    #: result in the browser instead. A plain synced field like any
    #: other -- flippable live, no camera reconstruction needed.
    overlay_client_render: bool = False

    def _overlay_ordered_widgets(self) -> list[tuple[str, HudWidget]]:
        """``_overlay_widgets`` items in paint order: lowest effective
        ``z_index`` first, so later items in the returned list are
        drawn on top of earlier ones. "Effective" because this camera's
        own ``overlay_widgets[topic]["z_index"]``, if present, overrides
        the widget's own :py:attr:`HudWidget.z_index` -- same override
        pattern as ``"x"``/``"y"``, see :py:attr:`overlay_widgets`.
        Stable, so widgets sharing a ``z_index`` keep their
        ``_overlay_widgets`` insertion order rather than being
        reshuffled every call.
        """
        return sorted(
            self._overlay_widgets.items(),
            key=lambda item: self.overlay_widgets.get(item[0], {}).get("z_index", item[1].z_index),
        )

    def _overlay_sync_widgets(self) -> None:
        """Reconcile mirrored widgets and mirrored data-source objects
        against the current ``overlay_widgets``.

        Only does network work (mirroring a widget, mirroring an object)
        the first time a topic is seen -- an already-mirrored widget or
        object is left alone and trusted to update itself live via its
        own SpiriSynq subscription, not re-fetched. That makes this cheap
        enough to call every frame (see :py:meth:`_render_overlays`),
        which is what actually keeps newly-added or newly-rebound
        entries picked up promptly without needing fine-grained
        ``EventedDict`` change signals wired up.

        Tolerant of a topic that does not resolve yet: it is retried on
        the next call rather than raising, since ``overlay_widgets`` is
        meant to be editable through a half-typed value the same way
        ``source_str`` is.
        """
        wanted = set(self.overlay_widgets.keys())

        for topic in list(self._overlay_widgets):
            if topic not in wanted:
                self._overlay_widgets.pop(topic).close()
                self._overlay_invalidate_widget_cache(topic)

        for topic in wanted:
            if topic in self._overlay_widgets:
                continue
            try:
                widget = self._overlay_fetch(
                    topic,
                    functools.partial(HudWidget.from_topic, topic, session=self.synq_session),
                )
            except Exception as exc:
                self._overlay_complain(topic, f"overlay topic unavailable, {exc}")
                continue
            if widget is None:
                continue
            self._overlay_widgets[topic] = widget
            self._overlay_complaints.pop(topic, None)
            # Population is lazy (`_cached_declared_objects`/
            # `_cached_resolve_objects`), but invalidation has to be
            # eager: this is the only place that ever sees the widget's
            # *old* template, right before a remote edit can replace it.
            widget.events.svg_template.connect(
                functools.partial(self._overlay_invalidate_widget_cache, topic)
            )

        self._overlay_sync_objects()

    def _overlay_invalidate_widget_cache(self, topic: str, *_args: object) -> None:
        """Drop a widget's cached template-parse results.

        Connected to that widget's own ``events.svg_template`` signal in
        :py:meth:`_overlay_sync_widgets` -- a widget's template is edited
        far less often than it is rendered, so :py:func:`declared_objects`
        and :py:func:`resolve_objects` (a regex scan plus a MiniJinja
        static-analysis pass) are worth caching per widget rather than
        re-running every frame; this is what keeps that cache correct
        across a live template edit instead of serving stale aliases.
        """
        self._overlay_declared_cache.pop(topic, None)
        self._overlay_resolved_cache.pop(topic, None)
        self._overlay_placement_cache.pop(topic, None)

    def _cached_declared_objects(self, topic: str, widget: HudWidget) -> dict[str, str]:
        """:py:func:`declared_objects`, cached until ``widget``'s template
        changes -- see :py:meth:`_overlay_invalidate_widget_cache`."""
        cached = self._overlay_declared_cache.get(topic)
        if cached is None:
            cached = declared_objects(widget.svg_template)
            self._overlay_declared_cache[topic] = cached
        return cached

    def _cached_resolve_objects(self, topic: str, widget: HudWidget) -> dict[str, str]:
        """:py:func:`resolve_objects`, cached until ``widget``'s template
        changes -- see :py:meth:`_overlay_invalidate_widget_cache`.

        Deliberately does not cache an :py:exc:`OverlayError` -- a
        template with a missing declaration should keep failing loudly
        (via :py:meth:`_overlay_complain`) on every call, not just once.
        """
        cached = self._overlay_resolved_cache.get(topic)
        if cached is None:
            cached = resolve_objects(widget.svg_template)
            self._overlay_resolved_cache[topic] = cached
        return cached

    def _cached_world_placement(self, topic: str, widget: HudWidget) -> WorldPlacement:
        """:py:func:`world_placement`, cached like :py:meth:`_cached_resolve_objects`,
        and like it, never caching an error."""
        cached = self._overlay_placement_cache.get(topic)
        if cached is None:
            cached = world_placement(widget.svg_template)
            self._overlay_placement_cache[topic] = cached
        return cached

    def _overlay_bound_topic(self, widget_topic: str, alias: str, default: str) -> str:
        """The object *alias* names for this camera: its binding, or *default*."""
        overrides = self.overlay_widgets.get(widget_topic, {}).get("bindings", {})
        return overrides.get(alias) or default

    def _overlay_linked_topics(self, widget_topic: str, widget: HudWidget) -> list[str]:
        """The topics an ``anchor: each`` widget's links name, in their order.

        Empty for any other widget, or while its object isn't mirrored yet.
        """
        try:
            placement = self._cached_world_placement(widget_topic, widget)
        except OverlayError:
            return []
        if placement.each is None:
            return []
        default = self._cached_declared_objects(widget_topic, widget).get(
            placement.anchor or "", ""
        )
        parent = self._overlay_objects.get(
            self._overlay_bound_topic(widget_topic, placement.anchor or "", default)
        )
        links = getattr(parent, placement.each, None) if parent is not None else None
        if not isinstance(links, (list, tuple)):
            return []
        # A Link, or a bare topic from a peer that sent one.
        return [
            topic
            for link in links
            if (topic := getattr(link, "topic", link)) and isinstance(topic, str)
        ]

    def _overlay_sync_objects(self) -> None:
        """Reconcile mirrored data-source objects against every mirrored
        widget's declared aliases and this camera's binding overrides.

        Companion to :py:meth:`_overlay_sync_widgets`, same "only fetch a
        topic the first time it's seen, then trust the subscription"
        policy -- see there for why.
        """
        wanted_topics: set[str] = set()
        for widget_topic, widget in self._overlay_widgets.items():
            usage = self.overlay_widgets.get(widget_topic, {})
            overrides = usage.get("bindings", {})
            for alias, default_topic in self._cached_declared_objects(widget_topic, widget).items():
                object_topic = overrides.get(alias) or default_topic
                if object_topic:
                    wanted_topics.add(object_topic)

        self._overlay_mirror(wanted_topics)
        # An `anchor: each` widget's linked objects, once the object holding
        # the links is mirrored -- so one frame later than it, at first.
        for widget_topic, widget in self._overlay_widgets.items():
            wanted_topics.update(self._overlay_linked_topics(widget_topic, widget))
        self._overlay_mirror(wanted_topics)

        for topic in list(self._overlay_objects):
            if topic not in wanted_topics:
                self._overlay_objects.pop(topic).close()

    def _overlay_mirror(self, topics: set[str]) -> None:
        """Mirror each of *topics* not mirrored yet, retrying failures next time."""
        for topic in topics:
            if topic in self._overlay_objects:
                continue
            try:
                obj = self._overlay_fetch(
                    topic, functools.partial(self.synq_session.from_topic_untyped, topic)
                )
            except Exception as exc:
                self._overlay_complain(topic, f"overlay object unavailable, {exc}")
                continue
            if obj is not None:
                self._overlay_objects[topic] = obj

    def _overlay_fetch[T](self, topic: str, fetch: Callable[[], T]) -> T | None:
        """``fetch()`` *topic*, unless it failed within :py:data:`_OVERLAY_RETRY_INTERVAL`.

        Returns
        -------
        T | None
            What ``fetch`` returned, or ``None`` while waiting to retry.

        Raises
        ------
        Exception
            Whatever ``fetch`` raised; *topic* is then not tried again
            until the interval has passed.
        """
        now = time.monotonic()
        if self._overlay_retry_at.get(topic, 0.0) > now:
            return None
        try:
            result = fetch()
        except Exception:
            self._overlay_retry_at[topic] = now + _OVERLAY_RETRY_INTERVAL
            raise
        self._overlay_retry_at.pop(topic, None)
        return result

    def _overlay_object_values_for(self, widget: HudWidget) -> dict[str, dict[str, object]]:
        """Latest known field values, keyed by alias, for each object
        ``widget`` declares and reads.

        Raises
        ------
        OverlayError
            Propagated from :py:func:`resolve_objects` if the template
            reads an alias it never declared.
        """
        widget_topic = widget.synq_absolute_path
        overrides = self.overlay_widgets.get(widget_topic, {}).get("bindings", {})

        values: dict[str, dict[str, object]] = {}
        for alias, default_topic in self._cached_resolve_objects(widget_topic, widget).items():
            object_topic = overrides.get(alias) or default_topic
            obj = self._overlay_objects.get(object_topic) if object_topic else None
            complaint_key = f"{widget_topic}#{alias}"
            if obj is None:
                self._overlay_complain(
                    complaint_key,
                    f"object {object_topic!r} unavailable for alias {alias!r}",
                )
                continue
            self._overlay_complaints.pop(complaint_key, None)
            values[alias] = _fields(obj)
        return values

    def _overlay_view(self, obj: object, width: int, height: int) -> View | None:
        """Where *obj* is in a ``width`` x ``height`` frame from this camera.

        None while anything it takes is unknown: this camera's pose or
        lens, or *obj*'s pose; or when *obj* is behind the camera.
        """
        camera, lens = getattr(self, "pose", None), getattr(self, "intrinsics", None)
        pose = getattr(obj, "pose", None)
        if isinstance(pose, Mapping):
            # A mirror whose session doesn't know Pose holds the plain value.
            pose = Pose.from_plain(dict(pose))
        if not (isinstance(camera, Pose) and isinstance(pose, Pose) and lens):
            return None
        return project(camera, lens, pose, width, height)

    def _overlay_renders(
        self,
        topic: str,
        widget: HudWidget,
        frame_info: Mapping[str, object],
        scale: tuple[float, float],
    ) -> list[tuple[str, tuple[float, float] | None]]:
        """Render *widget* for this frame: each copy's SVG, and where its centre goes.

        One copy, placed on the frame (``None``), for an ordinary or
        projecting widget; one per object it is anchored on that is in
        view, each centred on that object's pixel -- in the coordinate
        space the widgets are drawn in, *scale* times the frame's.  An
        anchored widget whose objects are all out of view has none.

        Raises
        ------
        OverlayError
            If the template can't be rendered, as :py:func:`render_svg`.
        """
        placement = self._cached_world_placement(topic, widget)
        objects = self._overlay_object_values_for(widget)
        width, height = int(frame_info["width"]), int(frame_info["height"])  # type: ignore[call-overload]

        def render(view: Mapping | None = None, item: Mapping | None = None) -> str:
            return render_svg(
                widget.svg_template,
                objects=objects,
                exif_tags=self.exif_tags,
                frame_info=frame_info,
                view=view,
                item=item,
            )

        if placement.project:
            views = {}
            for alias in placement.project:
                default = self._cached_declared_objects(topic, widget)[alias]
                obj = self._overlay_objects.get(self._overlay_bound_topic(topic, alias, default))
                view = self._overlay_view(obj, width, height) if obj is not None else None
                views[alias] = asdict(view) if view is not None else None
            return [(render(view=views), None)]
        if placement.anchor is None:
            return [(render(), None)]

        if placement.each is None:
            default = self._cached_declared_objects(topic, widget)[placement.anchor]
            targets = [self._overlay_bound_topic(topic, placement.anchor, default)]
        else:
            targets = self._overlay_linked_topics(topic, widget)
        renders = []
        for target in targets:
            obj = self._overlay_objects.get(target)
            view = self._overlay_view(obj, width, height) if obj is not None else None
            if view is None or not view.visible(width, height):
                continue
            sx, sy = scale
            centre = (view.x * sx, view.y * sy)
            # In the widget's own units, which are the canvas's.
            fields = asdict(view) | {
                "x": centre[0],
                "y": centre[1],
                "r": view.r * min(sx, sy) if view.r is not None else None,
            }
            item = _fields(obj) if placement.each is not None else None
            renders.append((render(view=fields, item=item), centre))
        return renders

    def _overlay_complain(self, key: str, message: str) -> None:
        """Log a failure once per distinct message, not once per frame."""
        if self._overlay_complaints.get(key) == message:
            return
        self._overlay_complaints[key] = message
        logger.warning(f"{self.synq_topic}: {message}")

    def _render_overlays(self, frame: np.ndarray) -> np.ndarray:
        """Composite every resolvable widget onto ``frame``.

        No-ops -- returns ``frame`` unchanged, same object -- when
        ``overlay_widgets`` is empty, so a camera with no overlays
        configured sees zero behavioural change from this mixin,
        including :py:meth:`~SpiriCamera.camera.Camera._encode`'s
        cache-by-identity optimization.

        Parameters
        ----------
        frame : np.ndarray
            The raw BGR frame about to be encoded.

        Returns
        -------
        np.ndarray
            ``frame`` with every resolvable widget baked in. A widget
            that fails to render this frame (bad template, a declared
            alias with no value yet, a thorvg parse failure) is skipped
            and logged once, not raised -- one broken overlay should
            never stop the camera from publishing frames.
        """
        self._overlay_sync_widgets()
        if not self._overlay_widgets:
            return frame

        frame_height, frame_width = frame.shape[:2]
        # The frame actually being rendered, not what was requested --
        # see the module docstring and camera_metrics_widget for why
        # this is injected directly rather than read through `objects`.
        frame_info = {
            "width": frame_width,
            "height": frame_height,
            "framerate": round(self.received_framerate, 1),
        }
        result = frame
        for topic, widget in self._overlay_ordered_widgets():
            try:
                for svg, centre in self._overlay_renders(topic, widget, frame_info, (1.0, 1.0)):
                    premultiplied, inverse_alpha = _rasterize_for_blend(
                        svg, frame_width, frame_height
                    )
                    widget_height, widget_width = inverse_alpha.shape[:2]
                    x, y = self._overlay_position(
                        topic,
                        widget,
                        centre,
                        frame_width,
                        frame_height,
                        widget_width,
                        widget_height,
                    )
                    # One copy of the frame, however many widgets: it is
                    # never blended onto in place -- see composite().
                    if result is frame:
                        result = frame.copy()
                    _blend_into(result, premultiplied, inverse_alpha, x, y)
            except OverlayError as exc:
                self._overlay_complain(topic, f"overlay {topic!r} not rendered, {exc}")
            else:
                self._overlay_complaints.pop(topic, None)

        return result

    def render_overlays_for_client(
        self,
        frame_width: int,
        frame_height: int,
        canvas_width: int | None = None,
        canvas_height: int | None = None,
    ) -> str:
        """SVG markup positioning every resolvable widget by itself, for
        a browser to render natively instead of :py:meth:`_render_overlays`
        rasterizing and compositing it server-side.

        Companion to :py:meth:`_render_overlays` for a camera with
        :py:attr:`overlay_client_render` set -- same widget iteration,
        same live object values, same :py:func:`anchor_position` math
        (so placement matches the server-rendered path pixel for pixel),
        but each widget is measured (:py:func:`measure_size`) rather
        than rasterized, then positioned with an SVG ``<g transform>``
        instead of being alpha-blended into a raster.

        Parameters
        ----------
        frame_width, frame_height : int
            The frame size overlays are anchored against -- typically
            ``self.received_width``/``self.received_height``, i.e. the
            size of the image the browser is actually displaying. Always
            what templates see as ``frame.width``/``frame.height``.
        canvas_width, canvas_height : int, optional
            The coordinate space widgets are measured and anchored in,
            when that differs from the frame -- e.g. the on-screen size
            of the displayed image in CSS pixels, so a widget drawn
            ``18`` units tall stays 18 screen pixels tall however large
            the frame is. Omitted -> the frame size, so widgets scale
            with the image exactly as they do when burned in
            server-side.

        Returns
        -------
        str
            Zero or more ``<g transform="translate(...)">...</g>``
            elements, one per resolvable widget, meant to be wrapped by
            the caller in a
            ``<svg viewBox="0 0 {canvas_width} {canvas_height}" width="100%"
            height="100%" preserveAspectRatio="xMidYMid meet">`` -- the
            explicit size and "meet" matter whenever the display box's
            aspect ratio can differ from the frame's (e.g. a resizable
            viewport), so this letterboxes the same way the ``<img>``
            does rather than stretching non-uniformly to fill the box.
            Empty when ``overlay_widgets`` is empty or no widget
            currently resolves -- same no-op contract as
            :py:meth:`_render_overlays`.
        """
        self._overlay_sync_widgets()
        if not self._overlay_widgets:
            return ""

        canvas_width = canvas_width or frame_width
        canvas_height = canvas_height or frame_height
        frame_info = {
            "width": frame_width,
            "height": frame_height,
            "framerate": round(self.received_framerate, 1),
        }
        scale = (canvas_width / frame_width, canvas_height / frame_height)
        pieces: list[str] = []
        for topic, widget in self._overlay_ordered_widgets():
            try:
                placed = []
                for svg, centre in self._overlay_renders(topic, widget, frame_info, scale):
                    widget_width, widget_height = measure_size(svg, canvas_width, canvas_height)
                    x, y = self._overlay_position(
                        topic,
                        widget,
                        centre,
                        canvas_width,
                        canvas_height,
                        widget_width,
                        widget_height,
                    )
                    sized_svg = _normalize_svg_size(svg, widget_width, widget_height)
                    placed.append(f'<g transform="translate({x},{y})">{sized_svg}</g>')
            except OverlayError as exc:
                self._overlay_complain(topic, f"overlay {topic!r} not rendered, {exc}")
            else:
                self._overlay_complaints.pop(topic, None)
                pieces.extend(placed)

        return "".join(pieces)

    def _overlay_position(
        self,
        topic: str,
        widget: HudWidget,
        centre: tuple[float, float] | None,
        width: int,
        height: int,
        widget_width: int,
        widget_height: int,
    ) -> tuple[int, int]:
        """A widget copy's top-left corner on a ``width`` x ``height`` canvas.

        Centred on *centre* for a copy anchored on an object; otherwise
        this camera's ``x``/``y`` override, or the widget's own anchor.
        """
        if centre is not None:
            return (
                round(centre[0] - widget_width / 2),
                round(centre[1] - widget_height / 2),
            )
        usage = self.overlay_widgets.get(topic, {})
        if "x" in usage and "y" in usage:
            return anchor_position(
                "custom",
                usage["x"],
                usage["y"],
                width,
                height,
                widget_width,
                widget_height,
            )
        return anchor_position(
            widget.anchor,
            widget.custom_anchor_x,
            widget.custom_anchor_y,
            width,
            height,
            widget_width,
            widget_height,
        )

    def _overlay_close(self) -> None:
        """Release every mirrored widget and mirrored data-source object."""
        for widget in self._overlay_widgets.values():
            widget.close()
        self._overlay_widgets.clear()
        for obj in self._overlay_objects.values():
            obj.close()
        self._overlay_objects.clear()
        self._overlay_retry_at.clear()
        self._overlay_declared_cache.clear()
        self._overlay_resolved_cache.clear()
        self._overlay_placement_cache.clear()


def _fields(obj: object) -> dict[str, object]:
    """A mirrored object's top-level synced fields, as a template reads them."""
    return {
        field_name: getattr(obj, field_name)
        for field_name in type(obj).valid_sync_paths()  # type: ignore[attr-defined]
        if "/" not in field_name
    }


__all__ = [
    "ANCHORS",
    "DEFAULT_FONT",
    "DEFAULT_FONT_PATH",
    "EXIF_NAME",
    "FRAME_NAME",
    "ITEM_NAME",
    "OBJECTS_NAME",
    "VIEW_NAME",
    "HudWidget",
    "OverlayError",
    "OverlayMixin",
    "WorldPlacement",
    "anchor_position",
    "camera_metrics_widget",
    "composite",
    "declared_objects",
    "rasterize",
    "render_svg",
    "resolve_objects",
    "tiger_widget",
    "world_placement",
]
