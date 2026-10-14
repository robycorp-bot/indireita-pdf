import asyncio
import io
import zipfile

import cv2
import numpy as np
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import BodySizeLimitMiddleware, create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def make_pdf(pages=1, encrypted=False, width=300, height=400):
    with pymupdf.open() as document:
        for _ in range(pages):
            page = document.new_page(width=width, height=height)
            for y in range(40, min(int(height), 380), 20):
                page.insert_text((30, y), "Documento de teste para melhoria.", fontsize=10)
        if encrypted:
            return document.tobytes(
                encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret"
            )
        return document.tobytes()


def upload(client, content, **options):
    data = {"dpi": "150", "auto_orient": "false", **options}
    return client.post(
        "/api/v1/pdfs/enhance", files={"file": ("original.pdf", content, "application/pdf")},
        data=data,
    )


def test_success_returns_readable_pdf_with_same_page_count(client):
    response = upload(client, make_pdf(pages=2))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-pages-processed"] == "2"
    assert response.headers["x-enhance-passes"] == "5"
    assert response.headers["cache-control"] == "no-store"
    assert "pdf-melhorado.pdf" in response.headers["content-disposition"]
    with pymupdf.open(stream=response.content, filetype="pdf") as result:
        assert result.page_count == 2
        assert result[0].get_images()
        assert result[0].rect.width == pytest.approx(300, abs=1)


def test_skewed_scan_is_corrected(client):
    image = np.full((800, 600), 255, dtype=np.uint8)
    for y in range(80, 750, 35):
        cv2.putText(image, "Texto torto no documento", (40, y), 0, 0.6, 0, 2)
    matrix = cv2.getRotationMatrix2D((300, 400), 6, 1)
    skewed = cv2.warpAffine(image, matrix, (600, 800), borderValue=255)
    _, png = cv2.imencode(".png", skewed)
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=400)
        page.insert_image(page.rect, stream=png.tobytes())
        pdf = document.tobytes()
    response = upload(client, pdf)
    assert response.status_code == 200
    assert response.headers["x-pages-deskewed"] == "1"


@pytest.mark.parametrize("content", [b"", b"not a pdf", b"%PDF-1.7\ncorrupted"])
def test_invalid_pdf(client, content):
    assert upload(client, content).status_code == 422


def test_password_protected_pdf(client):
    response = upload(client, make_pdf(encrypted=True))
    assert response.status_code == 422
    assert "senha" in response.json()["detail"]


@pytest.mark.parametrize("options", [
    {"dpi": "99"}, {"max_skew": "30"}, {"color_mode": "invalid"}, {"sharpen": "2"},
])
def test_invalid_parameters(client, options):
    assert upload(client, make_pdf(), **options).status_code == 422


def test_page_limit():
    with TestClient(create_app(Settings(max_pages=1))) as client:
        assert upload(client, make_pdf(pages=2)).status_code == 413


def test_pixel_limit():
    with TestClient(create_app(Settings(max_page_megapixels=1))) as client:
        assert upload(client, make_pdf(width=600, height=800)).status_code == 413


def test_upload_limit():
    with TestClient(create_app(Settings(max_upload_mb=1))) as client:
        assert upload(client, b"%PDF-1.7\n" + b"0" * (1024 * 1024)).status_code == 413


def test_request_body_limit():
    with TestClient(create_app(Settings(max_upload_mb=1))) as client:
        assert upload(client, b"0" * (3 * 1024 * 1024)).status_code == 413


def test_chunked_body_limit_without_content_length():
    messages = iter([
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
    ])

    async def receive():
        return next(messages)

    async def send(message):
        pass

    async def downstream(scope, receive, send):
        await receive()
        await receive()

    from starlette.exceptions import HTTPException

    middleware = BodySizeLimitMiddleware(downstream, max_bytes=5)
    with pytest.raises(HTTPException) as error:
        asyncio.run(middleware({"type": "http", "headers": []}, receive, send))
    assert error.value.status_code == 413


def test_swagger_and_openapi(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/pdfs/enhance"]["post"]
    assert "multipart/form-data" in operation["requestBody"]["content"]
    assert "application/pdf" in operation["responses"]["200"]["content"]
    zip_operation = schema["paths"]["/api/v1/pdfs/enhance-zip"]["post"]
    assert "application/zip" in zip_operation["responses"]["200"]["content"]
    grouping_operation = schema["paths"]["/api/v1/pdfs/enhance-and-group"]["post"]
    assert "application/zip" in grouping_operation["responses"]["200"]["content"]
    split_operation = schema["paths"]["/api/v1/pdfs/split"]["post"]
    assert "application/zip" in split_operation["responses"]["200"]["content"]
    assert client.get("/health").json()["status"] == "ok"


def test_zip_returns_processed_pdfs_and_preserves_other_files(client):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("entrada/primeiro.pdf", make_pdf())
        archive.writestr("notas.txt", b"manter")
    response = client.post(
        "/api/v1/pdfs/enhance-zip",
        files={"file": ("lote.zip", package.getvalue(), "application/zip")},
        data={"dpi": "150", "auto_orient": "false"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-pages-processed"] == "1"
    with zipfile.ZipFile(io.BytesIO(response.content)) as result:
        assert result.namelist() == ["entrada/primeiro.pdf", "notas.txt"]
        assert result.read("notas.txt") == b"manter"
        with pymupdf.open(stream=result.read("entrada/primeiro.pdf"), filetype="pdf") as pdf:
            assert pdf.page_count == 1


def test_zip_rejects_path_traversal(client):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../fora.pdf", make_pdf())
    response = client.post(
        "/api/v1/pdfs/enhance-zip",
        files={"file": ("lote.zip", package.getvalue(), "application/zip")},
    )
    assert response.status_code == 422
