"""
Single-endpoint orchestrator for the Loghi webservice stack.

Accepts one page image over HTTP and drives it through laypa (layout/baseline
detection) -> loghi-tooling (baseline extraction, line cutting, PageXML merge,
optional reading-order/word-split) -> htr (line transcription), returning the
finished PageXML (or plain text) in a single response.

This process itself has no GPU/ML dependencies - it only talks HTTP to the
laypa/htr/loghi-tooling containers and reads/writes the shared volumes they
already use, so it runs natively on the host.
"""
import os
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse, Response

LAYPA_URL = os.environ.get("LAYPA_URL", "http://localhost:5000")
HTR_URL = os.environ.get("HTR_URL", "http://localhost:5001")
TOOLING_URL = os.environ.get("TOOLING_URL", "http://localhost:8080")
DEFAULT_LAYPA_MODEL = os.environ.get("LAYPA_MODEL", "baseline2")

LAYPA_OUTPUT_BASE_PATH = Path(os.environ["LAYPA_OUTPUT_BASE_PATH"])
STORAGE_LOCATION = Path(os.environ["STORAGE_LOCATION"])
HTR_OUTPUT_PATH = Path(os.environ["HTR_OUTPUT_PATH"])
WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/loghi-orchestrator-work"))

POLL_INTERVAL = 0.5
DEFAULT_TIMEOUT = 600


app = FastAPI(title="Loghi Orchestrator")


class PipelineError(Exception):
    pass


def wait_for_finish(status_url_prefix: str, identifier: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Poll a Loghi service's /status/<identifier> endpoint until it reports finished/error."""
    deadline = time.monotonic() + timeout
    url = f"{status_url_prefix}/{identifier}"
    while time.monotonic() < deadline:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 404:
            time.sleep(POLL_INTERVAL)
            continue
        data = resp.json()
        status = data.get("status")
        if status == "finished":
            return data
        if status in ("error", "cancelled"):
            raise PipelineError(f"{url} reported {status}: {data}")
        time.sleep(POLL_INTERVAL)
    raise PipelineError(f"Timed out waiting for {url}")


def run_laypa(image_path: Path, identifier: str, model: str) -> None:
    with open(image_path, "rb") as f:
        resp = requests.post(
            f"{LAYPA_URL}/predict",
            files={"image": (image_path.name, f)},
            data={"identifier": identifier, "model": model},
            timeout=60,
        )
    if resp.status_code >= 400:
        raise PipelineError(f"laypa /predict failed: {resp.status_code} {resp.text}")
    wait_for_finish(f"{LAYPA_URL}/status", identifier)


def run_extract_baselines(image_path: Path, mask_path: Path, xml_path: Path, identifier: str) -> None:
    with open(image_path, "rb") as img_f, open(mask_path, "rb") as mask_f, open(xml_path, "rb") as xml_f:
        resp = requests.post(
            f"{TOOLING_URL}/extract-baselines",
            files={
                "mask": (mask_path.name, mask_f),
                "xml": (xml_path.name, xml_f),
                "image": (image_path.name, img_f),
            },
            data={"identifier": identifier},
            timeout=60,
        )
    if resp.status_code >= 400:
        raise PipelineError(f"extract-baselines failed: {resp.status_code} {resp.text}")
    wait_for_finish(f"{TOOLING_URL}/extract-baselines/status", identifier)


def run_cut_from_image(image_path: Path, page_xml_path: Path, identifier: str) -> None:
    with open(image_path, "rb") as img_f, open(page_xml_path, "rb") as page_f:
        resp = requests.post(
            f"{TOOLING_URL}/cut-from-image-based-on-page-xml-new",
            files={
                "image": (image_path.name, img_f),
                "page": (page_xml_path.name, page_f),
            },
            data={"identifier": identifier, "output_type": "png", "channels": "4"},
            timeout=60,
        )
    if resp.status_code >= 400:
        raise PipelineError(f"cut-from-image failed: {resp.status_code} {resp.text}")
    wait_for_finish(f"{TOOLING_URL}/cut-from-image-based-on-page-xml-new/status", identifier)


def run_htr_one(png_path: Path, group_id: str) -> str:
    line_identifier = png_path.stem
    with open(png_path, "rb") as f:
        resp = requests.post(
            f"{HTR_URL}/predict",
            files={"image": (png_path.name, f)},
            data=[("group_id", group_id), ("identifier", line_identifier),
                  ("whitelist", "model_name"), ("whitelist", "git_hash")],
            timeout=60,
        )
    if resp.status_code >= 400:
        raise PipelineError(f"htr /predict failed for {line_identifier}: {resp.status_code} {resp.text}")
    return line_identifier


def run_htr_all(snippet_dir: Path, group_id: str) -> Path:
    pngs = sorted(snippet_dir.glob("*.png"))
    if not pngs:
        raise PipelineError(f"No line snippets found in {snippet_dir}")

    line_identifiers = [run_htr_one(png, group_id) for png in pngs]
    for line_identifier in line_identifiers:
        wait_for_finish(f"{HTR_URL}/status", line_identifier)

    results_path = WORK_DIR / group_id / "results.txt"
    htr_group_dir = HTR_OUTPUT_PATH / group_id
    with open(results_path, "w", encoding="utf-8") as out:
        for line_identifier in line_identifiers:
            line_result = (htr_group_dir / f"{line_identifier}.txt").read_text(encoding="utf-8")
            out.write(line_result)
            if not line_result.endswith("\n"):
                out.write("\n")
    return results_path


def run_merge(page_xml_path: Path, results_path: Path, identifier: str) -> None:
    with open(page_xml_path, "rb") as page_f, open(results_path, "rb") as results_f:
        resp = requests.post(
            f"{TOOLING_URL}/loghi-htr-merge-page-xml",
            files={
                "page": (page_xml_path.name, page_f),
                "results": (results_path.name, results_f),
            },
            data={"identifier": identifier},
            timeout=60,
        )
    if resp.status_code >= 400:
        raise PipelineError(f"loghi-htr-merge-page-xml failed: {resp.status_code} {resp.text}")
    wait_for_finish(f"{TOOLING_URL}/loghi-htr-merge-page-xml/status", identifier)


def run_recalculate_reading_order(page_xml_path: Path, identifier: str) -> None:
    with open(page_xml_path, "rb") as page_f:
        resp = requests.post(
            f"{TOOLING_URL}/recalculate-reading-order-new",
            files={"page": (page_xml_path.name, page_f)},
            data={"identifier": identifier, "border_margin": "200"},
            timeout=60,
        )
    if resp.status_code >= 400:
        raise PipelineError(f"recalculate-reading-order-new failed: {resp.status_code} {resp.text}")
    wait_for_finish(f"{TOOLING_URL}/recalculate-reading-order-new/status", identifier)


def run_split_words(xml_path: Path, identifier: str) -> None:
    with open(xml_path, "rb") as xml_f:
        resp = requests.post(
            f"{TOOLING_URL}/split-page-xml-text-line-into-words",
            files={"xml": (xml_path.name, xml_f)},
            data={"identifier": identifier},
            timeout=60,
        )
    if resp.status_code >= 400:
        raise PipelineError(f"split-page-xml-text-line-into-words failed: {resp.status_code} {resp.text}")
    wait_for_finish(f"{TOOLING_URL}/split-page-xml-text-line-into-words/status", identifier)


def extract_plain_text(page_xml_path: Path) -> str:
    tree = ET.parse(page_xml_path)
    root = tree.getroot()
    ns_uri = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
    ns = {"pc": ns_uri}
    lines = []
    for text_line in root.iter(f"{{{ns_uri}}}TextLine"):
        unicode_el = text_line.find(".//pc:TextEquiv/pc:Unicode", ns)
        if unicode_el is not None and unicode_el.text:
            lines.append(unicode_el.text)
    return "\n".join(lines)


@app.get("/health")
def health():
    statuses = {}
    for name, url in (("laypa", f"{LAYPA_URL}/prometheus"),
                       ("htr", f"{HTR_URL}/prometheus"),
                       ("loghi-tooling", f"{TOOLING_URL.replace(':8080', ':8081')}/prometheus")):
        try:
            resp = requests.get(url, timeout=5)
            statuses[name] = "up" if resp.status_code == 200 else f"http {resp.status_code}"
        except requests.RequestException as exc:
            statuses[name] = f"unreachable ({exc.__class__.__name__})"
    healthy = all(v == "up" for v in statuses.values())
    return {"healthy": healthy, "services": statuses}


@app.post("/transcribe")
def transcribe(
    image: UploadFile = File(...),
    model: str = Form(DEFAULT_LAYPA_MODEL),
    recalculate_reading_order: bool = Form(True),
    split_words: bool = Form(False),
    output_format: str = Form("xml"),
    keep_intermediate: bool = Form(False),
):
    if output_format not in ("xml", "text"):
        raise HTTPException(400, "output_format must be 'xml' or 'text'")

    identifier = uuid.uuid4().hex
    work_dir = WORK_DIR / identifier
    work_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(image.filename or "image.jpg").suffix or ".jpg"
    image_path = work_dir / f"{identifier}{ext}"
    with open(image_path, "wb") as f:
        shutil.copyfileobj(image.file, f)

    try:
        run_laypa(image_path, identifier, model)

        laypa_page_dir = LAYPA_OUTPUT_BASE_PATH / identifier / "page"
        mask_path = laypa_page_dir / f"{identifier}.png"
        xml_path = laypa_page_dir / f"{identifier}.xml"

        run_extract_baselines(image_path, mask_path, xml_path, identifier)
        extracted_xml_path = STORAGE_LOCATION / identifier / f"{identifier}.xml"

        run_cut_from_image(image_path, extracted_xml_path, identifier)
        snippet_dir = STORAGE_LOCATION / identifier / identifier

        results_path = run_htr_all(snippet_dir, identifier)

        run_merge(extracted_xml_path, results_path, identifier)
        merged_xml_path = STORAGE_LOCATION / identifier / f"{identifier}.xml"

        if recalculate_reading_order:
            run_recalculate_reading_order(merged_xml_path, identifier)
        if split_words:
            run_split_words(merged_xml_path, identifier)

        final_xml_path = STORAGE_LOCATION / identifier / f"{identifier}.xml"
        if output_format == "text":
            return PlainTextResponse(extract_plain_text(final_xml_path))
        return Response(final_xml_path.read_bytes(), media_type="application/xml")
    except PipelineError as exc:
        raise HTTPException(502, str(exc))
    finally:
        if not keep_intermediate:
            shutil.rmtree(work_dir, ignore_errors=True)
            shutil.rmtree(LAYPA_OUTPUT_BASE_PATH / identifier, ignore_errors=True)
            shutil.rmtree(STORAGE_LOCATION / identifier, ignore_errors=True)
            shutil.rmtree(HTR_OUTPUT_PATH / identifier, ignore_errors=True)
