import asyncio
import logging
import multiprocessing
import tempfile
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.processing import (
    Options,
    ProcessingError,
    Report,
    process_and_group_pdf,
    process_pdf,
    split_pdf,
    process_zip,
    tesseract_command,
)

logger = logging.getLogger(__name__)


class BodySizeLimitMiddleware:
    """Limita o corpo inclusive quando nao ha Content-Length (upload chunked)."""

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            declared_size = int(headers.get(b"content-length", b"0"))
        except ValueError:
            response = JSONResponse({"detail": "Content-Length invalido."}, status_code=400)
            await response(scope, receive, send)
            return
        if declared_size > self.max_bytes:
            response = JSONResponse(
                {"detail": "Upload excede o limite permitido."}, status_code=413
            )
            await response(scope, receive, send)
            return
        consumed = 0

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise StarletteHTTPException(413, "Upload excede o limite permitido.")
            return message

        await self.app(scope, limited_receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.jobs = asyncio.Semaphore(settings.max_concurrent_jobs)
        # PyMuPDF nao suporta uso concorrente em threads. Isolamos jobs em processos.
        pool = ProcessPoolExecutor(
            max_workers=settings.max_concurrent_jobs,
            mp_context=multiprocessing.get_context("spawn"),
        )
        application.state.pool = pool
        try:
            yield
        finally:
            await run_in_threadpool(pool.shutdown, wait=True, cancel_futures=True)

    application = FastAPI(
        title="Endireita PDF",
        version="1.0.0",
        description=(
            "Corrige inclinacao e melhora a legibilidade de PDFs digitalizados. "
            "O resultado e rasterizado: texto selecionavel, links, formularios e assinaturas "
            "digitais nao sao preservados. Nao recupera detalhes ausentes no original. "
            "Use color_mode=color para preservar cores."
        ),
        lifespan=lifespan,
    )
    application.add_middleware(
        BodySizeLimitMiddleware, max_bytes=(settings.max_upload_mb + 1) * 1024 * 1024
    )

    async def process_upload(
        request: Request,
        file: UploadFile,
        options: Options,
        processor: object,
        source_name: str,
        target_name: str,
        media_type: str,
        download_name: str,
        signature: bytes,
        passes: int = 1,
    ) -> FileResponse:
        """Persiste um upload, executa jobs isolados encadeados e limpa temporarios.

        Quando passes > 1, a saida de cada passada alimenta a proxima — como se o
        usuario baixasse o resultado e reenviasse — mas apenas a ultima saida e
        devolvida. A passada para antecipadamente se nenhuma pagina precisou de
        correcao, pois nesse caso as passadas restantes seriam equivalentes a
        reenviar um documento ja estabilizado.
        """
        temporary = tempfile.TemporaryDirectory(prefix="endireita-pdf-")
        response_owns_directory = False
        acquired = False
        try:
            source = Path(temporary.name) / source_name
            target = Path(temporary.name) / target_name
            received = 0
            with source.open("wb") as destination:
                while chunk := await file.read(1024 * 1024):
                    received += len(chunk)
                    if received > settings.max_upload_mb * 1024 * 1024:
                        raise HTTPException(413, f"O limite e {settings.max_upload_mb} MB.")
                    destination.write(chunk)
            if received == 0:
                raise HTTPException(422, "O arquivo esta vazio.")
            with source.open("rb") as uploaded:
                if not uploaded.read(len(signature)).startswith(signature):
                    raise HTTPException(
                        422, "O conteudo enviado nao corresponde ao tipo informado."
                    )
            try:
                await asyncio.wait_for(
                    request.app.state.jobs.acquire(), timeout=settings.queue_timeout_seconds
                )
                acquired = True
            except TimeoutError as exc:
                raise HTTPException(
                    503, "Servidor ocupado. Tente novamente em alguns segundos.",
                    headers={"Retry-After": "5"},
                ) from exc
            report: Report | None = None
            passes_run = 0
            current = source
            final_path = target
            try:
                for pass_index in range(passes):
                    last_pass = pass_index == passes - 1
                    pass_target = target if last_pass else (
                        Path(temporary.name) / f"passada-{pass_index + 1:02d}.pdf"
                    )
                    future = asyncio.get_running_loop().run_in_executor(
                        request.app.state.pool, processor, current, pass_target,
                        options, settings,
                    )
                    try:
                        pass_report = await asyncio.shield(future)
                    except asyncio.CancelledError:
                        try:
                            await asyncio.shield(future)
                        except Exception:
                            logger.exception("Processamento falhou apos cancelamento da requisicao")
                        raise
                    passes_run += 1
                    if report is None:
                        report = pass_report
                    else:
                        report.pages = pass_report.pages
                        report.deskewed_pages += pass_report.deskewed_pages
                        report.reoriented_pages += pass_report.reoriented_pages
                        report.warnings |= pass_report.warnings
                    current = pass_target
                    final_path = pass_target
                    if (
                        passes == 1
                        and pass_report.deskewed_pages == 0
                        and pass_report.reoriented_pages == 0
                    ):
                        break
            except ProcessingError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            except Exception as exc:
                logger.exception("Falha no processamento")
                raise HTTPException(500, "Nao foi possivel processar o arquivo.") from exc
            response = FileResponse(
                final_path,
                media_type=media_type,
                filename=download_name,
                headers={
                    "X-Pages-Processed": str(report.pages),
                    "X-Pages-Deskewed": str(report.deskewed_pages),
                    "X-Pages-Reoriented": str(report.reoriented_pages),
                    "X-Enhance-Passes": str(passes_run),
                    "X-Processing-Warnings": ",".join(sorted(report.warnings)) or "none",
                    "Cache-Control": "no-store",
                },
                background=BackgroundTask(temporary.cleanup),
            )
            response_owns_directory = True
            return response
        finally:
            await file.close()
            if acquired:
                request.app.state.jobs.release()
            if not response_owns_directory:
                temporary.cleanup()

    @application.get("/health", tags=["Saude"])
    async def health() -> dict:
        executable = tesseract_command()
        # Nao chama um subprocesso a cada health check.
        return {"status": "ok", "orientation_detection_available": executable is not None}

    @application.post(
        "/api/v1/pdfs/enhance",
        tags=["PDF"],
        summary="Endireitar e melhorar um PDF digitalizado",
        response_class=FileResponse,
        responses={
            200: {
                "description": "PDF processado. Estatisticas e avisos nos cabecalhos X-*.",
                "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
            },
            413: {"description": "Limite de upload, paginas ou pixels excedido."},
            422: {"description": "Parametros invalidos, PDF invalido ou protegido por senha."},
            503: {"description": "Capacidade de processamento ocupada; tente novamente."},
        },
    )
    async def enhance_pdf(
        request: Request,
        file: Annotated[UploadFile, File(description="PDF original (maximo configuravel: 50 MB).")],
        dpi: Annotated[int, Form(ge=100, le=450, description="Resolucao de renderizacao.")] = 300,
        max_skew: Annotated[
            float, Form(ge=1, le=20, description="Limite da busca de inclinacao, em graus.")
        ] = 15.0,
        auto_orient: Annotated[
            bool, Form(description="Detectar paginas viradas em 90/180/270 graus com Tesseract.")
        ] = True,
        denoise: Annotated[bool, Form(description="Reducao moderada de ruido.")] = True,
        remove_shadows: Annotated[
            bool, Form(description="Compensar iluminacao desigual; desligue para fotografias.")
        ] = True,
        sharpen: Annotated[
            float, Form(ge=0, le=1.5, description="Intensidade da nitidez; 0 desativa.")
        ] = 0.5,
        color_mode: Annotated[
            str,
            Form(
                pattern="^(grayscale|color|blackwhite)$",
                description=(
                    "grayscale: tons de cinza; color: cores; blackwhite: texto preto/branco."
                ),
            ),
        ] = "grayscale",
    ) -> FileResponse:
        options = Options(dpi, max_skew, auto_orient, denoise, remove_shadows, sharpen, color_mode)
        return await process_upload(
            request, file, options, process_pdf, "input.pdf", "output.pdf", "application/pdf",
            "pdf-melhorado.pdf", b"%PDF-", passes=5,
        )

    @application.post(
        "/api/v1/pdfs/enhance-zip",
        tags=["PDF"],
        summary="Endireitar e melhorar todos os PDFs de um ZIP",
        response_class=FileResponse,
        responses={
            200: {
                "description": "ZIP devolvido com os PDFs processados no mesmo caminho interno.",
                "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
            },
            413: {
                "description": (
                    "Limite de upload, arquivos, descompactacao, paginas ou pixels excedido."
                )
            },
            422: {
                "description": "Parametros invalidos, ZIP/PDF invalido ou PDF protegido por senha."
            },
            503: {"description": "Capacidade de processamento ocupada; tente novamente."},
        },
    )
    async def enhance_zip(
        request: Request,
        file: Annotated[UploadFile, File(description="ZIP com PDFs (maximo configuravel: 50 MB).")],
        dpi: Annotated[int, Form(ge=100, le=450)] = 300,
        max_skew: Annotated[float, Form(ge=1, le=20)] = 15.0,
        auto_orient: Annotated[bool, Form()] = True,
        denoise: Annotated[bool, Form()] = True,
        remove_shadows: Annotated[bool, Form()] = True,
        sharpen: Annotated[float, Form(ge=0, le=1.5)] = 0.5,
        color_mode: Annotated[str, Form(pattern="^(grayscale|color|blackwhite)$")] = "grayscale",
    ) -> FileResponse:
        options = Options(dpi, max_skew, auto_orient, denoise, remove_shadows, sharpen, color_mode)
        return await process_upload(
            request, file, options, process_zip, "input.zip", "pdfs-melhorados.zip",
            "application/zip",
            "pdfs-melhorados.zip", b"PK",
        )

    @application.post(
        "/api/v1/pdfs/enhance-and-group",
        tags=["PDF"],
        summary="Endireitar, melhorar e agrupar as paginas de um PDF",
        response_class=FileResponse,
        responses={
            200: {
                "description": "ZIP com PDFs de paginas que compartilham o mesmo padrao.",
                "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
            },
            413: {"description": "Limite de upload, paginas ou pixels excedido."},
            422: {"description": "Parametros invalidos ou PDF invalido/protegido por senha."},
            503: {"description": "Capacidade de processamento ocupada; tente novamente."},
        },
    )
    async def enhance_and_group_pdf(
        request: Request,
        file: Annotated[UploadFile, File(description="PDF a tratar e separar por padrao.")],
        dpi: Annotated[int, Form(ge=100, le=450)] = 300,
        max_skew: Annotated[float, Form(ge=1, le=20)] = 15.0,
        auto_orient: Annotated[bool, Form()] = True,
        denoise: Annotated[bool, Form()] = True,
        remove_shadows: Annotated[bool, Form()] = True,
        sharpen: Annotated[float, Form(ge=0, le=1.5)] = 0.5,
        color_mode: Annotated[str, Form(pattern="^(grayscale|color|blackwhite)$")] = "grayscale",
    ) -> FileResponse:
        options = Options(dpi, max_skew, auto_orient, denoise, remove_shadows, sharpen, color_mode)
        return await process_upload(
            request, file, options, process_and_group_pdf, "input.pdf", "pdfs-agrupados.zip",
            "application/zip", "pdfs-agrupados.zip", b"%PDF-",
        )

    @application.post(
        "/api/v1/pdfs/split",
        tags=["PDF"],
        summary="Separar cada pagina de um PDF em um arquivo individual",
        response_class=FileResponse,
        responses={
            200: {
                "description": "ZIP contendo um PDF por pagina, com nomes baseados no texto detectado.",
                "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
            },
            413: {"description": "Limite de upload, paginas ou pixels excedido."},
            422: {"description": "Parametros invalidos, PDF invalido ou protegido por senha."},
            503: {"description": "Capacidade de processamento ocupada; tente novamente."},
        },
    )
    async def split_pdf_endpoint(
        request: Request,
        file: Annotated[UploadFile, File(description="PDF a separar em paginas individuais.")],
    ) -> FileResponse:
        return await process_upload(
            request, file, Options(), split_pdf, "input.pdf", "pdfs-separados.zip",
            "application/zip", "pdfs-separados.zip", b"%PDF-",
        )

    return application


app = create_app()
