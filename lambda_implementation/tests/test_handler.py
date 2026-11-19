import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from onedata_lambda_sdk.testing import build_request, run_local

from repository_download import handler
from repository_download.handler import handle


DOI = "10.12345/abcde-12345"
DIR_ID = "dirGuid"


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class _Repository(BaseHTTPRequestHandler):
    """Fake DOI resolver + Invenio repository; routes are set per test on the server."""

    def do_GET(self):
        route = self.server.routes.get(self.path)
        if route is None:
            self.send_error(404)
            return
        status, headers, body = route(self)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def repository(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Repository)
    server.routes = {}
    server.url = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(handler, "DOI_RESOLVER_URL", server.url)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def destination(tmp_path, monkeypatch):
    mount = tmp_path / "mount"
    directory = mount / f".__onedata__file_id__{DIR_ID}"
    directory.mkdir(parents=True)
    monkeypatch.setenv("ONECLIENT_MOUNT_POINT", str(mount))
    return directory


def _serve_record(server, archive: bytes, *, negotiates_json: bool = True):
    record = {
        "links": {
            "self_html": f"{server.url}/records/abcde-12345",
            "archive": f"{server.url}/api/records/abcde-12345/files-archive",
        }
    }
    record_json = json.dumps(record).encode()

    def landing_page(request):
        if negotiates_json and request.headers["Accept"] == "application/json":
            return 200, {"Content-Type": "application/json"}, record_json
        return 200, {"Content-Type": "text/html"}, b"<html>landing page</html>"

    server.routes = {
        f"/{DOI}": lambda _: (302, {"Location": "/records/abcde-12345"}, b""),
        "/records/abcde-12345": landing_page,
        "/api/records/abcde-12345": lambda _: (
            200,
            {"Content-Type": "application/json"},
            record_json,
        ),
        "/api/records/abcde-12345/files-archive": lambda _: (
            200,
            {"Content-Type": "application/zip"},
            archive,
        ),
    }


def _run_local(tmp_path, doi=DOI, destination_type="DIR"):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    args = {"doi": doi, "destination": {"fileId": DIR_ID, "type": destination_type}}
    return run_local(handle, build_request([args]), out_dir=out_dir)


def _run(tmp_path, doi=DOI, destination_type="DIR"):
    return _run_local(tmp_path, doi, destination_type).envelope


def _tree(directory) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("doi", [DOI, f"https://doi.org/{DOI}", f"doi:{DOI}", f"  {DOI}\n"])
def test_extracts_archive_into_destination(repository, destination, tmp_path, doi):
    _serve_record(repository, _zip({"readme.txt": b"hello", "data/a.csv": b"1,2,3"}))

    envelope = _run(tmp_path, doi)

    assert envelope == {
        "resultsBatch": [
            {
                "summary": {
                    "doi": DOI,
                    "recordUrl": f"{repository.url}/records/abcde-12345",
                    "filesCount": 2,
                    "totalBytes": 10,
                }
            }
        ]
    }
    assert _tree(destination) == {"readme.txt": b"hello", "data/a.csv": b"1,2,3"}


def test_extracts_zip_files_of_the_record_in_place(repository, destination, tmp_path):
    data_zip = _zip({"dataset/x.bin": b"xx", "dataset/inner.zip": b"kept as is"})
    _serve_record(repository, _zip({"DATA.ZIP": data_zip, "notes.txt": b"n"}))

    envelope = _run(tmp_path)

    assert envelope["resultsBatch"][0]["summary"]["filesCount"] == 3
    assert _tree(destination) == {
        "dataset/x.bin": b"xx",
        "dataset/inner.zip": b"kept as is",
        "notes.txt": b"n",
    }


def test_logs_progress_stages(repository, destination, tmp_path):
    _serve_record(repository, _zip({"readme.txt": b"hello", "data/a.csv": b"1,2,3"}))

    result = _run_local(tmp_path)

    assert result.streams["logs"] == [
        {"severity": "info", "content": content}
        for content in [
            f"Lambda initialized for DOI {DOI}",
            "Resolving DOI and fetching record metadata",
            f"Downloading dataset from {repository.url}/api/records/abcde-12345/files-archive",
            "Dataset downloaded (0.0 MB)",
            "Extracting dataset and uploading it to Onedata",
            "Finished: uploaded 2 files (0.0 MB)",
        ]
    ]


def test_logs_periodic_progress(repository, destination, tmp_path, monkeypatch):
    monkeypatch.setattr(handler, "PROGRESS_INTERVAL_SEC", 0.0)
    _serve_record(repository, _zip({"a.txt": b"a"}))

    contents = [entry["content"] for entry in _run_local(tmp_path).streams["logs"]]

    assert "Downloaded 0.0 MB" in contents
    assert "Uploaded 0 files (0.0 MB) to Onedata" in contents


def test_works_when_log_stream_is_not_writable(repository, destination, tmp_path, monkeypatch):
    _serve_record(repository, _zip({"a.txt": b"a"}))
    monkeypatch.setattr(handler.Logger, "info", lambda *_: (_ for _ in ()).throw(OSError()))

    envelope = _run(tmp_path)

    assert envelope["resultsBatch"][0]["summary"]["filesCount"] == 1
    assert _tree(destination) == {"a.txt": b"a"}


def test_falls_back_to_api_when_landing_page_is_html(repository, destination, tmp_path):
    _serve_record(repository, _zip({"a.txt": b"a"}), negotiates_json=False)

    envelope = _run(tmp_path)

    assert envelope["resultsBatch"][0]["summary"]["filesCount"] == 1
    assert _tree(destination) == {"a.txt": b"a"}


def test_rejects_archive_escaping_destination(repository, destination, tmp_path):
    _serve_record(repository, _zip({"../evil.txt": b"x"}))

    envelope = _run(tmp_path)

    assert "unsafe file path" in envelope["resultsBatch"][0]["exception"]
    assert not (destination.parent / "evil.txt").exists()


@pytest.mark.parametrize("doi", ["", "not-a-doi", "https://example.com/10.1/x", 5])
def test_rejects_invalid_doi(repository, destination, tmp_path, doi):
    envelope = _run(tmp_path, doi)

    assert "DOI" in envelope["resultsBatch"][0]["exception"]


def test_rejects_non_directory_destination(repository, destination, tmp_path):
    envelope = _run(tmp_path, destination_type="REG")

    assert "must be a directory" in envelope["resultsBatch"][0]["exception"]


def test_reports_unknown_doi(repository, destination, tmp_path):
    envelope = _run(tmp_path)  # the repository serves no routes

    assert "HTTP 404" in envelope["resultsBatch"][0]["exception"]


def test_reports_record_without_archive_link(repository, destination, tmp_path):
    repository.routes = {
        f"/{DOI}": lambda _: (200, {"Content-Type": "application/json"}, b'{"links": {}}')
    }

    envelope = _run(tmp_path)

    assert "links.archive" in envelope["resultsBatch"][0]["exception"]
