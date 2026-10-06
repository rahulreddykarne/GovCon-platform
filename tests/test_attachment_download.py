"""H2/H3: attachment refs, safe download, storage, provenance, and inventory versions."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from govcon.compliance.inventory import build_document_inventory
from govcon.config import Settings
from govcon.enrich.attachment_refs import (
    AttachmentRef,
    attachment_refs_for,
    resource_link_urls,
)
from govcon.enrich.attachments import (
    DOWNLOAD_FAILED,
    choose_filename,
    download_attachments,
    sanitize_filename,
    store_bytes,
)
from govcon.enrich.extract import (
    ExtractionLimitExceeded,
    ExtractionLimits,
    check_zip_container,
    extract_text,
)
from govcon.enrich.safe_fetch import FetchBlocked, FetchTooLarge, check_url, safe_fetch
from govcon.ingest.sam_opportunities import ingest_opportunity_records
from govcon.models import Opportunity, OpportunitySnapshot, Requirement, StoredFile

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "solicitation_fixture.pdf"
PUBLIC_IP = "93.184.216.34"


def public_resolver(_host: str) -> list[str]:
    return [PUBLIC_IP]


@pytest.fixture()
def session(upgraded_engine):
    with Session(upgraded_engine) as s:
        yield s
        s.rollback()


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(data_dir=tmp_path, sam_api_key="sam-test-key-123456", **overrides)


def _opp(session: Session, *, links: dict | None = None, raw: dict | None = None) -> Opportunity:
    row = Opportunity(
        source="sam",
        source_id=f"att-{uuid4().hex}",
        title="Attachment fixture",
        status="open",
        raw=raw or {},
        links=links or {},
    )
    session.add(row)
    session.flush()
    return row


def _client(routes: dict[str, httpx.Response], seen: list[httpx.Request] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        response = routes.get(key)
        return response if response is not None else httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _pdf_response(name: str = "Solicitation SPE4A6.pdf") -> httpx.Response:
    return httpx.Response(
        200,
        content=FIXTURE_PDF.read_bytes(),
        headers={"content-type": "application/pdf", "content-disposition": f'attachment; filename="{name}"'},
    )


# ── refs ──────────────────────────────────────────────────────────────────────


def test_refs_normalise_every_variant(session) -> None:
    opp = _opp(
        session,
        links={"attachments": ["https://api.sam.gov/prod/opportunities/v3/resources/files/a1/download"]},
        raw={
            "resourceLinks": [
                {"url": "https://api.sam.gov/prod/opportunities/v3/resources/files/a1/download", "name": "Main RFQ.pdf"},
                {"uri": "https://api.sam.gov/prod/opportunities/v3/resources/files/b2/download", "fileName": "SOW.docx"},
                "ftp://not-allowed.example/file",
                {"name": "no url"},
            ]
        },
    )
    refs = attachment_refs_for(opp)
    assert [r.url.rsplit("/", 2)[-2] for r in refs] == ["a1", "b2"]
    assert refs[0].filename == "Main RFQ.pdf"  # a named variant fills in the bare string's name
    assert refs[1].filename == "SOW.docx"


def test_dibbs_rows_list_their_rfq_pdf_never_the_daily_package(session) -> None:
    links = {"ui": "https://www.dibbs.bsm.dla.mil/RFQ/RfqRec.aspx?sn=X", "package": "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive/ca260925.zip"}
    opp = _opp(session, links=links)
    opp.source = "dibbs"
    assert attachment_refs_for(opp) == []  # no DLA solicitation number: nothing to fetch
    opp.solicitation_number = "SPE1C127T0007"
    [ref] = attachment_refs_for(opp)
    assert ref.url == "https://dibbs2.bsm.dla.mil/Downloads/RFQ/7/SPE1C127T0007.PDF"
    assert ref.filename == "SPE1C127T0007.pdf" and ref.source_metadata["origin"] == "dibbs_rfq_pdf"


def test_sam_ingest_keeps_object_shaped_resource_links() -> None:
    urls = resource_link_urls([{"url": "https://api.sam.gov/x/download", "name": "A"}, "https://api.sam.gov/y/download"])
    assert urls == ["https://api.sam.gov/x/download", "https://api.sam.gov/y/download"]


# ── safe fetch ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "192.168.0.10", "172.16.5.4", "169.254.169.254", "::1", "fd00::1", "0.0.0.0", "::ffff:127.0.0.1"],
)
def test_private_and_metadata_addresses_are_blocked(address: str) -> None:
    with pytest.raises(FetchBlocked):
        check_url("https://files.example.test/a.pdf", resolver=lambda _h: [address])


def test_literal_ip_and_plain_http_are_checked() -> None:
    with pytest.raises(FetchBlocked):
        check_url("https://127.0.0.1/a.pdf", resolver=public_resolver)
    with pytest.raises(FetchBlocked):
        check_url("http://files.example.test/a.pdf", resolver=public_resolver)
    check_url("http://files.example.test/a.pdf", resolver=public_resolver, allow_http=True)
    with pytest.raises(FetchBlocked):
        check_url("https://user:pw@files.example.test/a.pdf", resolver=public_resolver)


def test_redirect_to_a_private_address_is_blocked() -> None:
    def resolver(host: str) -> list[str]:
        return ["10.0.0.5"] if host == "internal.example.test" else [PUBLIC_IP]

    client = _client({"https://files.example.test/a": httpx.Response(302, headers={"location": "https://internal.example.test/secret"})})
    with pytest.raises(FetchBlocked):
        safe_fetch(client, "https://files.example.test/a", max_bytes=1000, resolver=resolver, attempts=1)


def test_api_key_is_not_forwarded_to_another_host() -> None:
    seen: list[httpx.Request] = []
    client = _client(
        {
            "https://api.sam.gov/files/1/download": httpx.Response(302, headers={"location": "https://bucket.example.test/obj"}),
            "https://bucket.example.test/obj": httpx.Response(200, content=b"ok"),
        },
        seen,
    )
    result = safe_fetch(client, "https://api.sam.gov/files/1/download", max_bytes=100, resolver=public_resolver, params={"api_key": "k"})
    assert result.content == b"ok"
    assert "api_key" in str(seen[0].url)
    assert "api_key" not in str(seen[1].url)


def test_body_is_streamed_with_a_byte_cap() -> None:
    def stream():
        for _ in range(10):
            yield b"x" * 100

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=stream())))
    with pytest.raises(FetchTooLarge):
        safe_fetch(client, "https://files.example.test/big", max_bytes=250, resolver=public_resolver, attempts=1)
    declared = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 10, headers={"content-length": "999999"})))
    with pytest.raises(FetchTooLarge):
        safe_fetch(declared, "https://files.example.test/big", max_bytes=250, resolver=public_resolver, attempts=1)


# ── filenames and storage ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../escaped.txt", "escaped.txt"),
        ("..\\..\\win.txt", "win.txt"),
        ("/etc/passwd", "passwd"),
        ("..", "attachment"),
        ("a%2F..%2Fb.pdf", "b.pdf"),
        ("ok name (1).pdf", "ok name _1_.pdf"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_content_disposition_names_generic_download_urls() -> None:
    ref = AttachmentRef(url="https://api.sam.gov/prod/opportunities/v3/resources/files/abc/download")
    assert choose_filename(ref, "attachment; filename*=UTF-8''Price%20Schedule.xlsx", None) == "Price Schedule.xlsx"
    assert choose_filename(ref, None, "application/pdf") == "attachment.pdf"


def test_store_bytes_stays_inside_the_folder_and_never_overwrites(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = store_bytes(b"one", hashlib.sha256(b"one").hexdigest(), 7, "../escaped.txt", settings)
    folder = (tmp_path / "attachments" / "7").resolve()
    assert first.parent == folder and first.read_bytes() == b"one"
    assert not (tmp_path / "attachments" / "escaped.txt").exists()
    again = store_bytes(b"one", hashlib.sha256(b"one").hexdigest(), 7, "escaped.txt", settings)
    assert again == first
    other = store_bytes(b"two", hashlib.sha256(b"two").hexdigest(), 7, "escaped.txt", settings)
    assert other != first and first.read_bytes() == b"one" and other.read_bytes() == b"two"


# ── download, provenance, failures, versions ─────────────────────────────────


def test_download_names_stores_and_records_provenance(session, tmp_path: Path) -> None:
    url_a = "https://api.sam.gov/prod/opportunities/v3/resources/files/aaa/download"
    url_b = "https://api.sam.gov/prod/opportunities/v3/resources/files/bbb/download"
    opp = _opp(session, links={"attachments": [url_a, url_b]})
    snapshot = OpportunitySnapshot(opportunity_id=opp.id, content_hash=uuid4().hex, raw={})
    session.add(snapshot)
    session.flush()
    seen: list[httpx.Request] = []
    client = _client(
        {
            "https://api.sam.gov/prod/opportunities/v3/resources/files/aaa/download": _pdf_response("RFQ main.pdf"),
            "https://api.sam.gov/prod/opportunities/v3/resources/files/bbb/download": httpx.Response(
                200, content=b"Offerors shall include a signed SF 1449.", headers={"content-type": "text/plain; charset=utf-8"}
            ),
        },
        seen,
    )
    files = download_attachments(session, opp, settings=_settings(tmp_path), client=client, resolver=public_resolver)
    assert len(files) == 2
    names = {f.filename for f in files}
    assert names == {"RFQ main.pdf", "attachment.txt"}
    paths = {f.local_path for f in files}
    assert len(paths) == 2  # two "/download" URLs no longer overwrite each other
    for row in files:
        assert row.snapshot_id == snapshot.id
        assert row.downloaded_at is not None
        assert row.url in {url_a, url_b} and "api_key" not in row.url
        assert Path(row.local_path).resolve().parent == (tmp_path / "attachments" / str(opp.id)).resolve()
    pdf = next(f for f in files if f.mime_type == "application/pdf")
    assert pdf.extraction_status == "success" and "SPE4A6-26-Q-0042" in pdf.extracted_text
    assert all("api_key=sam-test-key-123456" in str(r.url) for r in seen)


def test_failed_download_is_recorded_and_blocks_the_inventory(session, tmp_path: Path) -> None:
    url = "https://files.example.test/missing.pdf"
    opp = _opp(session, links={"attachments": [url]})
    files = download_attachments(session, opp, settings=_settings(tmp_path), client=_client({}), resolver=public_resolver)
    assert len(files) == 1 and files[0].extraction_status == DOWNLOAD_FAILED and files[0].sha256 is None
    assert "HTTP 404" in files[0].extraction_error
    inventory, _run = build_document_inventory(session, opp.id)
    codes = {w.code: w for w in inventory.warnings}
    assert codes["download_failed"].blocking is True
    assert codes["missing_expected_attachment"].blocking is True
    assert not inventory.complete

    # A later successful download reuses the failure row.
    ok = download_attachments(
        session, opp, settings=_settings(tmp_path), client=_client({url: _pdf_response()}), resolver=public_resolver
    )
    assert ok[0].id == files[0].id and ok[0].sha256 and ok[0].extraction_status == "success"


def test_ssrf_url_is_recorded_as_failure_not_fetched(session, tmp_path: Path) -> None:
    opp = _opp(session, links={"attachments": ["https://metadata.example.test/latest"]})
    calls: list[httpx.Request] = []
    files = download_attachments(
        session, opp, settings=_settings(tmp_path), client=_client({}, calls), resolver=lambda _h: ["169.254.169.254"]
    )
    assert files[0].extraction_status == DOWNLOAD_FAILED and calls == []


def test_removed_and_replaced_attachments_become_inactive(session, tmp_path: Path) -> None:
    url_keep = "https://files.example.test/keep.txt"
    url_drop = "https://files.example.test/drop.txt"
    opp = _opp(session, links={"attachments": [url_keep, url_drop]})
    v1 = {url_keep: httpx.Response(200, content=b"version one shall apply", headers={"content-type": "text/plain"}),
          url_drop: httpx.Response(200, content=b"dropped later", headers={"content-type": "text/plain"})}
    first = download_attachments(session, opp, settings=_settings(tmp_path), client=_client(v1), resolver=public_resolver)
    keep_v1 = next(f for f in first if f.url == url_keep)

    opp.links = {"attachments": [url_keep]}
    session.flush()
    v2 = {url_keep: httpx.Response(200, content=b"version two shall apply", headers={"content-type": "text/plain"})}
    second = download_attachments(session, opp, settings=_settings(tmp_path), client=_client(v2), resolver=public_resolver)
    keep_v2 = second[0]
    assert keep_v2.id != keep_v1.id

    rows = {r.id: r for r in session.scalars(select(StoredFile).where(StoredFile.opportunity_id == opp.id)).all()}
    assert rows[keep_v2.id].active is True
    assert rows[keep_v1.id].active is False and rows[keep_v1.id].removed_at is not None
    dropped = next(r for r in rows.values() if r.url == url_drop)
    assert dropped.active is False

    inventory, _ = build_document_inventory(session, opp.id)
    assert [d.file_id for d in inventory.documents] == [keep_v2.id]


def test_tampered_retained_file_is_a_blocking_integrity_problem(session, tmp_path: Path) -> None:
    url = "https://files.example.test/rfq.pdf"
    opp = _opp(session, links={"attachments": [url]})
    files = download_attachments(session, opp, settings=_settings(tmp_path), client=_client({url: _pdf_response()}), resolver=public_resolver)
    Path(files[0].local_path).write_bytes(b"tampered")
    inventory, _ = build_document_inventory(session, opp.id)
    warning = next(w for w in inventory.warnings if w.code == "file_integrity_mismatch")
    assert warning.blocking is True
    assert inventory.documents[0].page_texts is None  # tampered bytes were not used


# ── extraction limits ────────────────────────────────────────────────────────


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_zip_bomb_docx_is_rejected_before_parsing() -> None:
    bomb = _zip_bytes({"word/document.xml": b"0" * 5_000_000})
    with pytest.raises(ExtractionLimitExceeded):
        check_zip_container(bomb)
    result = extract_text(bomb, "", "bomb.docx")
    assert result.status == "error" and "rejected before parsing" in result.error
    many = _zip_bytes({f"f{i}.xml": b"x" for i in range(30)})
    with pytest.raises(ExtractionLimitExceeded):
        check_zip_container(many, ExtractionLimits(max_zip_members=10))


def test_pdf_pages_and_text_are_capped() -> None:
    data = FIXTURE_PDF.read_bytes()
    paged = extract_text(data, "application/pdf", "a.pdf", ExtractionLimits(max_pdf_pages=1))
    assert paged.status == "partial" and "truncated at 1 of 2 pages" in paged.error
    capped = extract_text(b"y" * 500, "text/plain", "a.txt", ExtractionLimits(max_text_chars=100))
    assert capped.status == "partial" and len(capped.text) == 100


# ── end to end: SAM response → snapshot → download → extraction → compliance ─


def test_realistic_sam_attachment_flows_into_compliance(session, tmp_path: Path) -> None:
    from govcon.compliance.pipeline import run_compliance_pipeline

    notice = f"e2e-{uuid4().hex}"
    file_url = f"https://api.sam.gov/prod/opportunities/v3/resources/files/{uuid4().hex}/download"
    record = {
        "noticeId": notice,
        "title": "Surgical Gloves, Nitrile",
        "solicitationNumber": "SPE4A6-26-Q-0042",
        "fullParentPathName": "DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY",
        "postedDate": "2026-09-20",
        "type": "Combined Synopsis/Solicitation",
        "active": "Yes",
        "classificationCode": "6515",
        "naicsCode": "339112",
        "typeOfSetAside": "SBA",
        "responseDeadLine": "2027-01-15T14:00:00-05:00",
        "uiLink": f"https://sam.gov/opp/{notice}/view",
        "description": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=" + notice,
        "resourceLinks": [file_url],
    }
    stats = ingest_opportunity_records(session, [record])
    assert stats.inserted == 1
    opp = session.scalar(select(Opportunity).where(Opportunity.source_id == notice))
    snapshot_id = session.scalar(select(OpportunitySnapshot.id).where(OpportunitySnapshot.opportunity_id == opp.id))
    assert opp.links["attachments"] == [file_url]

    description_route = "https://api.sam.gov/prod/opportunities/v1/noticedesc"
    files = download_attachments(
        session,
        opp,
        settings=_settings(tmp_path),
        client=_client({file_url: _pdf_response("SPE4A6-26-Q-0042 Solicitation.pdf"),
                        description_route: httpx.Response(200, json={"description": "<p>Nitrile gloves.</p>"})}),
        resolver=public_resolver,
    )
    assert [f.filename for f in files] == ["SPE4A6-26-Q-0042 Solicitation.pdf", "SAM notice description.txt"]
    stored = files[0]
    assert stored.extraction_status == "success" and stored.snapshot_id == snapshot_id
    assert stored.filename == "SPE4A6-26-Q-0042 Solicitation.pdf"

    result = run_compliance_pipeline(session, opp.id, use_ai=False)
    assert result["inventory"]["documents"] == 2
    requirements = session.scalars(select(Requirement).where(Requirement.opportunity_id == opp.id)).all()
    assert requirements, "the downloaded solicitation produced no requirements"
    assert any(r.source_file_id == stored.id for r in requirements)
    assert all(r.source_snapshot_id in (None, snapshot_id) for r in requirements)


# ── source documents the feeds point to (SAM descriptions, DIBBS RFQ PDFs) ──

DESCRIPTION_URL = "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=abc123"


def test_sam_notice_lists_its_description_and_dla_rfq_pdf(session) -> None:
    opp = _opp(session, links={"description": DESCRIPTION_URL})
    opp.solicitation_number = "SPE4A727T0080"
    refs = attachment_refs_for(opp)
    assert [(r.source_metadata["origin"], r.url) for r in refs] == [
        ("sam_description", DESCRIPTION_URL),
        ("dibbs_rfq_pdf", "https://dibbs2.bsm.dla.mil/Downloads/RFQ/0/SPE4A727T0080.PDF"),
    ]
    # A notice with its own SAM attachments carries its documents: no DIBBS guess.
    opp.links = {"description": DESCRIPTION_URL, "attachments": ["https://api.sam.gov/x/files/a1/download"]}
    assert [r.source_metadata["origin"] for r in attachment_refs_for(opp)] == ["links.attachments", "sam_description"]
    # Not a DLA solicitation number: no DIBBS address is invented.
    opp.links, opp.solicitation_number = {"description": DESCRIPTION_URL}, "W912DY-26-R-0001"
    assert [r.source_metadata["origin"] for r in attachment_refs_for(opp)] == ["sam_description"]


def test_sam_description_keeps_its_notice_id_and_is_stored_as_text(session, tmp_path: Path) -> None:
    opp = _opp(session, links={"description": DESCRIPTION_URL})
    seen: list[httpx.Request] = []
    body = {"description": "<p>Line 0001 Qty 103&nbsp;EA</p><ul><li>Approved sources: 18350 218229</li></ul>"}
    [row] = download_attachments(
        session, opp, settings=_settings(tmp_path),
        client=_client({"https://api.sam.gov/prod/opportunities/v1/noticedesc": httpx.Response(200, json=body)}, seen),
        resolver=public_resolver,
    )
    assert seen[0].url.params["noticeid"] == "abc123"  # the key is merged in, not swapped for the query
    assert seen[0].url.params["api_key"] == "sam-test-key-123456"
    assert row.extraction_status == "success" and row.mime_type == "text/plain"
    assert row.extracted_text == "Line 0001 Qty 103 EA\nApproved sources: 18350 218229"
    assert row.url == DESCRIPTION_URL and "api_key" not in row.url
    assert row.classification == "PUBLIC" and row.source_origin == "government_feed"


def _dibbs_client(final: httpx.Response) -> httpx.Client:
    """DIBBS: the banner until consent is posted, then ``final``."""
    from test_dibbs import WARNING_HTML

    def handler(request: httpx.Request) -> httpx.Response:
        return final if request.method == "POST" else httpx.Response(200, text=WARNING_HTML)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _dibbs_opp(session):
    opp = _opp(session)
    opp.source, opp.solicitation_number = "dibbs", "SPE1C127T0007"
    return opp


def test_dibbs_rfq_pdf_is_fetched_through_the_consent_banner_and_read(session, tmp_path: Path) -> None:
    pdf = httpx.Response(200, content=FIXTURE_PDF.read_bytes(), headers={"content-type": "application/pdf"})
    [row] = download_attachments(session, _dibbs_opp(session), client=_dibbs_client(pdf),
                                 settings=_settings(tmp_path, dibbs_request_interval_seconds=0))
    assert row.extraction_status == "success" and row.mime_type == "application/pdf"
    assert row.filename == "SPE1C127T0007.pdf" and row.page_count and row.extracted_text


@pytest.mark.parametrize("final, reason", [
    (httpx.Response(200, text="<html><h1>File was not found</h1></html>"), "web page instead of the solicitation PDF"),
    (httpx.Response(404, text="missing"), "no solicitation PDF at this address"),
])
def test_a_dibbs_page_that_is_not_the_pdf_is_a_recorded_failure(session, tmp_path: Path, final, reason) -> None:
    [row] = download_attachments(session, _dibbs_opp(session), client=_dibbs_client(final),
                                 settings=_settings(tmp_path, dibbs_request_interval_seconds=0))
    assert row.extraction_status == DOWNLOAD_FAILED and row.sha256 is None
    assert reason in row.extraction_error


def test_sam_documents_stored_for_the_current_notice_version_are_not_fetched_again(session, tmp_path: Path) -> None:
    """Re-fetching unchanged SAM documents spent the API quota (HTTP 429) and blocked the inventory."""
    notice = f"reuse-{uuid4().hex}"
    file_url = f"https://api.sam.gov/prod/opportunities/v3/resources/files/{uuid4().hex}/download"
    record = {
        "noticeId": notice, "title": "Valve", "solicitationNumber": "W912DY-26-Q-0001",
        "postedDate": "2026-09-20", "type": "Solicitation", "active": "Yes",
        "responseDeadLine": "2027-01-15T14:00:00-05:00",
        "description": "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid=" + notice,
        "resourceLinks": [file_url],
    }
    ingest_opportunity_records(session, [record])
    opp = session.scalar(select(Opportunity).where(Opportunity.source_id == notice))
    first = download_attachments(session, opp, settings=_settings(tmp_path), resolver=public_resolver, client=_client({
        file_url: _pdf_response(),
        "https://api.sam.gov/prod/opportunities/v1/noticedesc": httpx.Response(200, json={"description": "Valve, 3 EA."}),
    }))
    assert all(row.sha256 for row in first)
    # A later re-fetch failed and left an active failure row for the description.
    stale = StoredFile(opportunity_id=opp.id, url=record["description"], extraction_status=DOWNLOAD_FAILED,
                       extraction_error="download failed: HTTP 429", classification="PUBLIC",
                       source_origin="government_feed", active=True)
    session.add(stale)
    session.flush()

    seen: list[httpx.Request] = []
    again = download_attachments(session, opp, settings=_settings(tmp_path), resolver=public_resolver,
                                 client=_client({}, seen))  # every request would 404
    assert seen == []
    assert [row.id for row in again] == [row.id for row in first]
    session.refresh(stale)
    assert stale.active is False
    assert build_document_inventory(session, opp.id)[0].complete
