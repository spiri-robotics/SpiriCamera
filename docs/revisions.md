# Revision history

This table is generated from `revisions.yaml` at the root of this repository.
Do not edit it here -- add an entry there and it appears below, on the cover
page, and in the PDF footer.

```{revision-history}
```

## Issuing a revision

The document's revision is separate from the software's version: revise the
document when what it says changes.

1. Add an entry to the bottom of `revisions.yaml`, without `draft: true`.
1. Merge it. The next software release (`vX.Y.Z` tag) attaches the PDF of this
   revision to its release. That release asset is the controlled copy: link
   people to it rather than to a PDF built from a branch, which can change
   under them.
