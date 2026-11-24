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

A ready-made definition is in
[`repository download 1.3.json`](repository%20download%201.3.json): a lambda
dump exported from Onedata that can be uploaded into an automation inventory
instead of filling the lambda in by hand. It points at the image
`registry.gitlab.ics.muni.cz:443/485733/onedata-automation-repository-download:1.3`
and declares the arguments and results listed below.

To define the lambda manually, register the image in your automation inventory
with these settings:

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
| `logs` | Object, relayed via **file pipe** | Progress log entries (`{"severity", "content": {"description"}}`) |

To see the progress while the lambda runs, map the `logs` result to the task
audit log (or the workflow audit log) in the workflow's task definition. The
lambda reports each stage — initialized, resolving the DOI, downloading the
dataset, extracting and uploading it to Onedata, finished — and, during the
download and the upload, the amount transferred so far every 30 seconds.

Extraction and upload are a single stage: the archive is extracted directly
into the Onedata directory. If the `logs` result is not declared, the lambda
still works, only without progress reporting.

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

## Acknowledgment
<p align="left">
  <img src="https://webcentrum.muni.cz/media/3831863/seda_eosc.png" alt="EOSC CZ Logo" height="90">
</p>

---
This project output was developed with financial contributions from the [EOSC CZ](https://www.eosc.cz/projekty/narodni-podpora-pro-eosc) initiative throught the project **National Repository Platform for Research Data** (CZ.02.01.01/00/23_014/0008787) funded by Programme Johannes Amos Comenius (P JAC) of the Ministry of Education, Youth and Sports of the Czech Republic (MEYS).

---

<p align="left">
  <img src="https://webcentrum.muni.cz/media/3832168/seda_eu-msmt_eng.png" alt="EU and MŠMT Logos" height="90">
</p>
