# XLSX terminal sandbox

The XLSX image extends the pinned Linux amd64 Hermes terminal sandbox. It
installs openpyxl, et-xmlfile, and defusedxml into the image at
`/opt/hermes-xlsx`. The requirements file pins the exact PyPI wheels and their
SHA-256 hashes. Recreating a container from the image retains the interpreter
and libraries.

With installation approval, run this from the checkout root on the Docker
daemon that serves the target profile.

```bash
docker build --platform linux/amd64 --pull=false \
  -f docker/sandbox-xlsx.Dockerfile \
  -t hermes-sandbox-xlsx:3.1.5 docker
docker image inspect hermes-sandbox-xlsx:3.1.5 --format '{{.Id}}'
```

To validate from the checkout, run the focused tests with the candidate image.
They create fresh network-disabled Hermes Docker sandboxes with disposable
state and mount only the workbook scripts.

```bash
export HERMES_TEST_XLSX_IMAGE=hermes-sandbox-xlsx:3.1.5
UV_CACHE_DIR=/tmp/uv-cache uv run --with pytest pytest tests/tools/test_v2_xlsx_runtime.py -q
```

Before selecting the image, save the profile's exact configuration and record
its current image reference and image ID. Set `terminal.docker_image` to the
reviewed image reference in that profile's configuration after rollout
approval. Keep its existing network, mounts, and sandbox settings. If the
profile uses a shared container key, review every member of that shared
container before changing the image. Inside the Docker `terminal`, set
`PY=/opt/hermes-xlsx/bin/python` and use the Microsoft Office skill's XLSX
helpers. The build needs PyPI wheel access. Workbook reads and writes run
offline.

For rollback, restore the saved profile configuration and use its prior image.
Verify the next terminal session selected that image. Retain the prior image
and the profile's workspace. A change to the configured image may create a
different container, so check for active work before switching or rolling back.

The dedicated interpreter uses defusedxml for XML parsing. Formula evaluation
requires a separate engine such as LibreOffice. This image does not install or
enable one.
