"""
Onedata Automation lambda: download a dataset from an Invenio-based repository.

For each job it resolves the DOI to the repository record, downloads the record's
files archive (`links.archive` of the record metadata), and extracts it into the
given Onedata directory through the mounted Oneclient. Record files that are
themselves zip archives are extracted in place instead of being stored as `.zip`.
"""

import os
import re
import tempfile
import time
import zipfile
from typing import TypedDict
from urllib.parse import urlsplit

import requests
from onedata_lambda_sdk import AtmFile, Job, JobContext, JobException, per_job
from onedata_lambda_sdk.logging import Logger
from onedata_lambda_sdk.oneclient import mounted_file_path


DOI_RESOLVER_URL = "https://doi.org"
REQUEST_TIMEOUT = (30, 300)  # (connect, read) seconds
CHUNK_SIZE = 4 * 1024**2

LOG_STREAM = "logs"  # must equal a filePipe result named "logs" in the lambda schema
PROGRESS_INTERVAL_SEC = 30.0

_DOI_PREFIXES = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:)", re.IGNORECASE)
_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")
_LANDING_PAGE_PATH = re.compile(r"/records/(?P<id>[^/?#]+)")


class JobArgs(TypedDict):
    doi: str
    destination: AtmFile


class Summary(TypedDict):
    doi: str
    recordUrl: str
    filesCount: int
    totalBytes: int


class JobResult(TypedDict):
    summary: Summary


@per_job(max_workers=1)
def handle(job: Job[JobArgs], ctx: JobContext[dict]) -> JobResult:
    progress = _Progress(ctx)
    doi = _normalize_doi(job.args["doi"])
    destination = _destination_path(job.args["destination"])
    progress.stage(f"Lambda initialized for DOI {doi}")

    with requests.Session() as session, tempfile.TemporaryDirectory() as tmp_dir:
        progress.stage("Resolving DOI and fetching record metadata")
        record = _fetch_record(session, doi)
        archive_url = (record.get("links") or {}).get("archive")
        if not archive_url:
            raise JobException(f"Record of DOI {doi} has no files archive link (links.archive)")

        progress.stage(f"Downloading dataset from {archive_url}")
        archive_path = os.path.join(tmp_dir, "archive.zip")
        downloaded = _download(session, archive_url, archive_path, progress)
        progress.stage(f"Dataset downloaded ({_megabytes(downloaded)})")

        progress.stage("Extracting dataset and uploading it to Onedata")
        files_count, total_bytes = _extract(archive_path, destination, tmp_dir, progress)
        progress.stage(f"Finished: uploaded {files_count} files ({_megabytes(total_bytes)})")

    return {
        "summary": {
            "doi": doi,
            "recordUrl": (record.get("links") or {}).get("self_html") or archive_url,
            "filesCount": files_count,
            "totalBytes": total_bytes,
        }
    }


class _Progress:
    """
    Reports progress to the lambda's audit-log stream and keeps the job alive.

    Stage changes are logged immediately; `update` is called per copied chunk and logs
    at most once per `PROGRESS_INTERVAL_SEC`. Logging never fails the job: if the log
    stream cannot be written (e.g. the `logs` result is not declared in the lambda
    schema), progress reporting is switched off and the work continues.
    """

    def __init__(self, ctx: JobContext[dict]) -> None:
        self._heartbeat = ctx.heartbeat
        self._logger: Logger | None = ctx.logger(LOG_STREAM)
        self._last_update = time.monotonic()
        self.files = 0
        self.bytes = 0

    def stage(self, message: str) -> None:
        self._last_update = time.monotonic()
        self._log(message)

    def update(self, message: str) -> None:
        self._heartbeat()
        if time.monotonic() - self._last_update >= PROGRESS_INTERVAL_SEC:
            self.stage(message)

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger.info(message)
        except OSError:
            self._logger = None


def _megabytes(size: int) -> str:
    return f"{size / 1024**2:.1f} MB"


def _normalize_doi(raw: object) -> str:
    """Accepts `10.x/y`, `doi:10.x/y` and `https://doi.org/10.x/y`; returns `10.x/y`."""
    if not isinstance(raw, str):
        raise JobException(f"DOI must be a string, got: {raw!r}")
    doi = _DOI_PREFIXES.sub("", raw.strip())
    if not _DOI_PATTERN.match(doi):
        raise JobException(f"Not a valid DOI: {raw!r}")
    return doi


def _destination_path(destination: AtmFile) -> str:
    file_type = destination.get("type")
    if file_type is not None and file_type != "DIR":
        raise JobException(f"Destination must be a directory, got file of type {file_type}")
    path = mounted_file_path(destination["fileId"])
    if not os.path.isdir(path):
        raise JobException(f"Destination directory is not accessible: {path}")
    return path


def _fetch_record(session: requests.Session, doi: str) -> dict:
    """
    Resolve the DOI and return the record metadata JSON.

    Invenio answers with the record JSON when the DOI is followed with
    `Accept: application/json`. If the repository serves the HTML landing page
    anyway, fall back to the standard `/api/records/<id>` endpoint.
    """
    headers = {"Accept": "application/json"}
    response = _get(session, f"{DOI_RESOLVER_URL}/{doi}", f"resolve DOI {doi}", headers=headers)
    record = _json_object(response)
    if record is None:
        landing = urlsplit(response.url)
        match = _LANDING_PAGE_PATH.search(landing.path)
        if not match:
            raise JobException(
                f"DOI {doi} resolved to {response.url}, which is not an Invenio record"
            )
        api_url = f"{landing.scheme}://{landing.netloc}/api/records/{match['id']}"
        response = _get(session, api_url, f"fetch record metadata of DOI {doi}", headers=headers)
        record = _json_object(response)
        if record is None:
            raise JobException(f"{api_url} did not return record metadata JSON")
    return record


def _json_object(response: requests.Response) -> dict | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _get(session: requests.Session, url: str, action: str, **kwargs) -> requests.Response:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as ex:
        raise JobException(f"Failed to {action}: {ex}") from ex
    if not response.ok:
        response.close()
        raise JobException(f"Failed to {action}: HTTP {response.status_code} from {response.url}")
    return response


def _download(
    session: requests.Session, url: str, target_path: str, progress: _Progress
) -> int:
    response = _get(session, url, "download files archive", stream=True)
    downloaded = 0
    try:
        with response, open(target_path, "wb") as target:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                target.write(chunk)
                downloaded += len(chunk)
                progress.update(f"Downloaded {_megabytes(downloaded)}")
    except requests.RequestException as ex:
        raise JobException(f"Download of files archive was interrupted: {ex}") from ex
    return downloaded


def _extract(
    archive_path: str, destination: str, tmp_dir: str, progress: _Progress
) -> tuple[int, int]:
    """
    Extract the record's files archive into `destination`.

    Returns (number of files written, their total size). Record files that are
    zip archives are extracted into the directory they would have been stored in.
    """
    with _open_zip(archive_path, "files archive") as archive:
        for member in archive.infolist():
            if member.is_dir() or not member.filename.lower().endswith(".zip"):
                _extract_member(archive, member, destination, progress)
                continue

            nested_path = os.path.join(tmp_dir, "nested.zip")
            with archive.open(member) as source, open(nested_path, "wb") as target:
                while chunk := source.read(CHUNK_SIZE):
                    target.write(chunk)
                    progress.update(f"Unpacking {member.filename}")
            nested_destination = os.path.dirname(_target_path(destination, member.filename))
            with _open_zip(nested_path, member.filename) as nested:
                for nested_member in nested.infolist():
                    _extract_member(nested, nested_member, nested_destination, progress)
            os.remove(nested_path)
    return progress.files, progress.bytes


def _open_zip(path: str, name: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as ex:
        raise JobException(f"{name} is not a valid zip file: {ex}") from ex


def _extract_member(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo, destination: str, progress: _Progress
) -> None:
    target_path = _target_path(destination, member.filename)
    if member.is_dir():
        os.makedirs(target_path, exist_ok=True)
        return
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with archive.open(member) as source, open(target_path, "wb") as target:
        while chunk := source.read(CHUNK_SIZE):
            target.write(chunk)
            progress.bytes += len(chunk)
            progress.update(
                f"Uploaded {progress.files} files ({_megabytes(progress.bytes)}) to Onedata"
            )
    progress.files += 1


def _target_path(destination: str, member_name: str) -> str:
    """Path of a zip member under `destination`; refuses names escaping it."""
    parts = [part for part in member_name.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise JobException(f"Archive contains an unsafe file path: {member_name!r}")
    return os.path.join(destination, *parts)
