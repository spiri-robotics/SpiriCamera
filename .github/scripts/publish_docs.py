"""Publish one version of the docs into the GitHub Pages site.

The site lives on the gh-pages branch, so a version stays published once it is:

    index.html      redirects to latest/
    404.html        sends a link from before the docs were versioned to latest/
    versions.json   what's published: read by the version switcher in the
                    sidebar and by the org's home page
    latest/         a copy of the newest release; of dev until there is one
    vX.Y.Z/         each release, its PDF beside the HTML
    dev/            main, its PDF beside the HTML

    python .github/scripts/publish_docs.py SITE VERSION HTML [PDF]

VERSION is a release tag (vX.Y.Z) or "dev". Run by .github/workflows/docs.yml,
which commits SITE to gh-pages; this file comes from the project template.
Standard library only, so it runs on the runner's own Python.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

DEV = "dev"
LATEST = "latest"
# A pre-release (v1.0.0-rc1) is published, but never becomes latest.
RELEASE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)([-+.\w]*)$")


def publish(site: Path, version: str, html_dir: Path, pdf: Path | None = None) -> None:
    """Put *html_dir* (and *pdf*) at *site*/*version*, replacing what was there."""
    if version != DEV and not RELEASE.match(version):
        raise ValueError(f"{version!r} is neither a vX.Y.Z tag nor {DEV!r}")
    target = site / version
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(html_dir, target)
    if pdf is not None:
        shutil.copy2(pdf, target / pdf.name)
    refresh(site)


def _releases(site: Path) -> list[str]:
    """Every published release, newest first."""
    found = [(m, p.name) for p in site.iterdir() if p.is_dir() and (m := RELEASE.match(p.name))]
    # A final release sorts after its own pre-releases.
    found.sort(key=lambda f: (*map(int, f[0].groups()[:3]), f[0][4] == ""), reverse=True)
    return [name for _, name in found]


def _pdf(site: Path, version: str) -> str | None:
    pdfs = sorted((site / version).glob("*.pdf"))
    return f"{version}/{pdfs[0].name}" if pdfs else None


def refresh(site: Path) -> None:
    """Rewrite latest/, versions.json and the root pages from what's published."""
    releases = _releases(site)
    names = releases + ([DEV] if (site / DEV).is_dir() else [])
    final = [r for r in releases if not RELEASE.match(r)[4]]
    latest = final[0] if final else (DEV if DEV in names else None)

    shutil.rmtree(site / LATEST, ignore_errors=True)
    if latest:
        shutil.copytree(site / latest, site / LATEST)

    versions = {
        "latest": latest,
        "versions": [{"name": n, "path": f"{n}/", "pdf": _pdf(site, n)} for n in names],
    }
    (site / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
    (site / "index.html").write_text(INDEX)
    (site / "404.html").write_text(NOT_FOUND.replace("@VERSIONS@", json.dumps([LATEST, *names])))
    # Served as is: Jekyll would drop Sphinx's _static/ and _sources/.
    (site / ".nojekyll").touch()


INDEX = f"""<!doctype html>
<meta charset="utf-8">
<title>Redirecting</title>
<meta http-equiv="refresh" content="0; url={LATEST}/">
<link rel="canonical" href="{LATEST}/">
<p><a href="{LATEST}/">Go to the latest documentation</a></p>
"""

# GitHub Pages serves this for any missing path in the project's site. A path
# without a version in it is a link from before versioning: send it to the same
# page in latest/. Anything else really is missing.
NOT_FOUND = """<!doctype html>
<meta charset="utf-8">
<title>Page not found</title>
<script>
  const [, project, first, ...rest] = location.pathname.split("/");
  if (!@VERSIONS@.includes(first)) {
    location.replace(`/${project}/latest/${[first, ...rest].join("/")}${location.hash}`);
  }
</script>
<h1>Page not found</h1>
<p>This version of the documentation has no such page.
<a href="/" id="home">Go to the latest documentation</a>.</p>
<script>
  document.getElementById("home").href = `/${location.pathname.split("/")[1]}/latest/`;
</script>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("site", type=Path, help="the gh-pages checkout")
    parser.add_argument("version", help=f"a vX.Y.Z tag, or {DEV!r} for main")
    parser.add_argument("html", type=Path, help="the built HTML")
    parser.add_argument("pdf", type=Path, nargs="?", help="the built PDF")
    args = parser.parse_args()
    publish(args.site, args.version, args.html, args.pdf)


if __name__ == "__main__":
    main()
