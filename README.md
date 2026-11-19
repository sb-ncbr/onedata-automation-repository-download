# onedata-automation-repository-download

An [Onedata Automation](https://onedata.org) lambda that downloads a dataset
from an Invenio-based repository, identified by its DOI, and extracts it into
an Onedata directory. It is built on
[`onedata-lambda-sdk`](https://pypi.org/project/onedata-lambda-sdk/) (v3 lambda
model, Oneprovider 21.02.5 or newer).

## What it does

For each job the lambda:

1. Resolves the DOI (`https://doi.org/<doi>`) and reads the record metadata
   JSON of the repository record it points to.
2. Downloads the record's files archive from the `links.archive` URL of that
   metadata.
3. Extracts the archive into the destination directory through the mounted
   Oneclient.

The files archive is a zip wrapping all files of the record. Record files that
are themselves zip archives (for example a single `DATA.ZIP`) are extracted in
place rather than stored as `.zip`. Zip files nested deeper than that are kept
as they are. Existing files with the same path are overwritten.

## Lambda definition

Register the image as a lambda in your automation inventory with these
settings:

| Setting | Value |
|---------|-------|
| Docker image | the image built below |
| Mount Oneclient | **yes** |
| Read-only | **no** |
| Preferred batch size | 1 |

Arguments:

| Name | Type | Description |
|------|------|-------------|
| `doi` | String | DOI of the record: `10.48700/y5en5-qk548`, `doi:10.48700/y5en5-qk548` or `https://doi.org/10.48700/y5en5-qk548` |
| `destination` | File (directory) | Onedata directory the dataset is extracted into |

Results:

| Name | Type | Description |
|------|------|-------------|
| `summary` | Object | `{"doi", "recordUrl", "filesCount", "totalBytes"}` of the extracted dataset |

The archive is downloaded to the container's temporary directory before it is
extracted, so the lambda needs ephemeral storage of about twice the size of the
record's files, and enough time: the example record below (232 MB zipped, 2826
files) takes about two minutes on a local disk.

Only publicly accessible records can be downloaded; the lambda sends no
credentials to the repository.

## Build

```bash
cd lambda_implementation
docker build -t <registry>/repository-download:<tag> .
docker push <registry>/repository-download:<tag>
```

The `Dockerfile` is the canonical `lambda-base` template and should not be
edited. Dependencies are installed from `uv.lock`; run `uv lock` after changing
them in `pyproject.toml`.

## Development

```bash
cd lambda_implementation
uv sync
uv run pytest
```

The tests run the handler through the real SDK runtime against a local fake
repository, with the Oneclient mount pointed at a temporary directory.

To try it against a real record without Onedata:

```python
import os, tempfile

from onedata_lambda_sdk.testing import build_request, run_local
from repository_download.handler import handle

mount = tempfile.mkdtemp()
os.makedirs(f"{mount}/.__onedata__file_id__DIR")
os.environ["ONECLIENT_MOUNT_POINT"] = mount

args = {
    "doi": "https://doi.org/10.48700/y5en5-qk548",
    "destination": {"fileId": "DIR", "type": "DIR"},
}
print(run_local(handle, build_request([args]), out_dir=tempfile.mkdtemp()).envelope)
print("extracted to", f"{mount}/.__onedata__file_id__DIR")
```
