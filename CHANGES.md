# Changes

## Unreleased

### Improvements

- The image is built from SpiriProjectTemplates' Dockerfile: uv pinned, a `dev` stage the
  tests run in, and the camera-debug-ui's NiceGUI installed as a regular dependency rather
  than the `ui` extra.
- `docker/compose.yaml` runs the camera (`SpiriCamera run`, defaulting to the test pattern) on
  the `spirisynq` network, named after the host, instead of the old template's placeholder.
- Docs are published to GitHub Pages instead of Read the Docs, and are a controlled document
  (spiri-docs): a document number and revision, and a PDF attached to every release.

### Internal

- Adopted SpiriProjectTemplates (`.copier-answers.yml`): its CI, ruff config, Makefile and
  weekly template update. The `docs` extra is now a dependency group.
- Code formatted and linted to the template's ruff config.
