"""Processamento conservador de scans; nao usa IA generativa nem inventa detalhes."""

import logging
import math
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytesseract
from numpy.typing import NDArray

from app.config import Settings

logger = logging.getLogger(__name__)
Image = NDArray[np.uint8]
ORIENTATION_MIN_CONFIDENCE = 5.0
# Evita que cada requisicao crie dezenas de threads nativas do OpenCV.
cv2.setNumThreads(1)


class ProcessingError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Options:
    dpi: int = 300
    max_skew: float = 15.0
    auto_orient: bool = True
    denoise: bool = True
    remove_shadows: bool = True
    sharpen: float = 0.5
    color_mode: str = "grayscale"


@dataclass
class Report:
    pages: int = 0
    deskewed_pages: int = 0
    reoriented_pages: int = 0
    warnings: set[str] = field(default_factory=set)


def resize_for_analysis(image: Image, max_side: int) -> Image:
    height, width = image.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale == 1:
        return image
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def tesseract_command() -> str | None:
    """Localiza o Tesseract inclusive na instalacao padrao do Windows.

    O instalador UB Mannheim nem sempre atualiza o PATH da sessao que iniciou
    o Uvicorn. Procurar o local padrao evita que o usuario tenha de alterar os
    parametros do endpoint ou reiniciar o terminal somente para orientar scans.
    """
    if executable := shutil.which("tesseract"):
        return executable
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\\Program Files"))
        executable = program_files / "Tesseract-OCR" / "tesseract.exe"
        if executable.is_file():
            return str(executable)
    return None


def detect_orientation(image: Image, report: Report) -> int:
    """Retorna a rotacao horaria indicada pelo OSD, apenas com confianca suficiente."""
    executable = tesseract_command()
    if executable is None:
        report.warnings.add("orientation_unavailable")
        return 0
    pytesseract.pytesseract.tesseract_cmd = executable
    preview = resize_for_analysis(image, 2200)
    try:
        result = pytesseract.image_to_osd(
            preview, output_type=pytesseract.Output.DICT, config="--dpi 150", timeout=12
        )
    except (pytesseract.TesseractError, RuntimeError, OSError):
        # Paginas em branco, fotos ou pouco texto frequentemente nao permitem OSD.
        report.warnings.add("orientation_undetermined")
        return 0
    rotation = int(result.get("rotate", 0))
    confidence = float(result.get("orientation_conf", 0))
    # OSD e propositalmente usado com um limiar moderado: em scans reais a
    # confianca raramente chega a 10, embora a rotacao esteja correta. Ainda
    # exigimos uma rotacao de quadrante e confianca positiva para nao girar
    # paginas com pouco ou nenhum texto por palpite.
    if confidence >= ORIENTATION_MIN_CONFIDENCE and rotation in (90, 180, 270):
        return rotation
    if confidence < ORIENTATION_MIN_CONFIDENCE:
        report.warnings.add("orientation_low_confidence")
    return 0


def rotate_quadrant(image: Image, clockwise_degrees: int) -> Image:
    return np.ascontiguousarray(np.rot90(image, k=-(clockwise_degrees // 90)))


def estimate_skew(gray: Image, max_angle: float = 15.0) -> float:
    """Busca a correcao que alinha as linhas de texto por perfil de projecao.

    A energia das diferencas entre linhas cresce quando as linhas de texto ficam
    horizontais. A busca grosseira/fina dispensa OCR e nao depende do idioma.
    Retorna o angulo de CORRECAO usado pelo OpenCV, nao o angulo do documento.
    """
    sample = resize_for_analysis(gray, 1000)
    height, width = sample.shape
    if min(height, width) < 50:
        return 0.0
    # Margens e bordas do scanner nao devem dirigir a estimativa.
    margin_y, margin_x = max(1, height // 30), max(1, width // 30)
    sample = sample[margin_y:-margin_y, margin_x:-margin_x]
    _, binary = cv2.threshold(sample, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    keep = np.zeros(count, dtype=np.uint8)
    area = binary.size
    for index in range(1, count):
        x, y, w, h, pixels = stats[index]
        # Remove ruido isolado, grandes fotos e molduras conectadas a bordas.
        touches_edge = x == 0 or y == 0 or x + w == binary.shape[1] or y + h == binary.shape[0]
        if pixels >= 6 and pixels < area * 0.08 and not touches_edge:
            keep[index] = 1
    mask = keep[labels]
    ink = int(mask.sum())
    if ink < 200 or ink > area * 0.4:
        return 0.0
    h, w = mask.shape
    center = (w / 2, h / 2)
    # Uma busca mais ampla evita escolher um falso pico quando a inclinacao real
    # esta fora do limite solicitado. A correcao ainda respeita max_angle.
    search_limit = max(20.0, max_angle)
    # Padding impede cortar conteudo durante a medicao das projecoes.
    padding = int(math.ceil(max(h, w) * math.sin(math.radians(search_limit)))) + 2
    canvas = cv2.copyMakeBorder(
        mask, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=0
    )
    center = (center[0] + padding, center[1] + padding)

    def score(angle: float) -> float:
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(
            canvas, matrix, (canvas.shape[1], canvas.shape[0]), flags=cv2.INTER_NEAREST
        )
        projection = rotated.sum(axis=1, dtype=np.float64)
        return float(np.square(np.diff(projection)).sum())

    baseline = score(0)
    candidates = np.unique(np.append(np.arange(-search_limit, search_limit + 0.01, 1.0), 0))
    scores = [score(float(angle)) for angle in candidates]
    best = float(candidates[int(np.argmax(scores))])
    fine = np.arange(max(-search_limit, best - 1), min(search_limit, best + 1) + 0.01, 0.1)
    fine_scores = [score(float(angle)) for angle in fine]
    best_index = int(np.argmax(fine_scores))
    correction = float(fine[best_index])
    # Ganho pequeno e angulos muito pequenos geralmente correspondem a ruido.
    if abs(correction) < 0.2 or fine_scores[best_index] < baseline * 1.12:
        return 0.0
    # Exige um pico claro em relacao aos candidatos, nao apenas ao angulo zero.
    if fine_scores[best_index] < float(np.median(scores)) * 1.5:
        return 0.0
    if abs(correction) > max_angle or abs(correction) >= search_limit - 0.05:
        return 0.0
    return round(correction, 2)


def rotate_without_cropping(image: Image, angle: float) -> Image:
    if angle == 0:
        return image
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_width = math.ceil(height * sin + width * cos)
    new_height = math.ceil(height * cos + width * sin)
    matrix[0, 2] += (new_width - width) / 2
    matrix[1, 2] += (new_height - height) / 2
    border = (255, 255, 255) if image.ndim == 3 else 255
    return cv2.warpAffine(
        image,
        matrix,
        (new_width, new_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border,
    )


def enhance_image(image: Image, options: Options) -> Image:
    """Trata luminancia e preserva crominancia no modo colorido."""
    if options.color_mode == "color":
        lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB)
        luminance, a, b = cv2.split(lab)
    else:
        luminance = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    if options.remove_shadows:
        # Estima iluminacao em baixa resolucao: custo limitado mesmo a 300 DPI.
        small = resize_for_analysis(luminance, 1000)
        background = cv2.morphologyEx(
            small, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (35, 35))
        )
        background = cv2.GaussianBlur(background, (0, 0), 7)
        background = cv2.resize(
            background, (luminance.shape[1], luminance.shape[0]), interpolation=cv2.INTER_LINEAR
        )
        luminance = cv2.divide(luminance, np.maximum(background, 1), scale=255)
    if options.denoise:
        # Bilateral preserva contornos; filtragem moderada evita apagar tracos finos.
        luminance = cv2.bilateralFilter(luminance, 5, 18, 18)
    luminance = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(luminance)
    if options.sharpen > 0:
        blur = cv2.GaussianBlur(luminance, (0, 0), 1)
        luminance = cv2.addWeighted(
            luminance, 1 + options.sharpen, blur, -options.sharpen, 0
        )
    if options.color_mode == "color":
        return cv2.cvtColor(cv2.merge((luminance, a, b)), cv2.COLOR_LAB2RGB)
    if options.color_mode == "blackwhite":
        return cv2.adaptiveThreshold(
            luminance, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 15
        )
    return luminance


def process_pdf(source: Path, target: Path, options: Options, settings: Settings) -> Report:
    report = Report()
    try:
        # Abrir por stream evita handles presos no Windows em PDFs corrompidos.
        document = pymupdf.open(stream=source.read_bytes(), filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise ProcessingError("PDF invalido, corrompido ou nao suportado.") from exc
    with document, pymupdf.open() as output:
        if not document.is_pdf:
            raise ProcessingError("O arquivo precisa ser um PDF.")
        if document.needs_pass:
            raise ProcessingError("PDF protegido por senha nao e suportado.")
        if not document.page_count:
            raise ProcessingError("O PDF nao tem paginas.")
        if document.page_count > settings.max_pages:
            raise ProcessingError(f"Limite de {settings.max_pages} paginas excedido.", 413)
        # Validacao antes de renderizar evita alocacoes gigantes em paginas malformadas.
        total_pixels = 0
        for page in document:
            rect = page.rect
            if not all(math.isfinite(v) and v > 0 for v in (rect.width, rect.height)):
                raise ProcessingError("O PDF contem uma pagina com dimensoes invalidas.")
            width = math.ceil(rect.width * options.dpi / 72)
            height = math.ceil(rect.height * options.dpi / 72)
            # Rotacao arbitraria expande o canvas. Inclui essa folga no limite.
            radians = math.radians(options.max_skew)
            expanded_width = math.ceil(width * math.cos(radians) + height * math.sin(radians))
            expanded_height = math.ceil(height * math.cos(radians) + width * math.sin(radians))
            pixels = max(width * height, expanded_width * expanded_height)
            if pixels > settings.max_page_megapixels * 1_000_000:
                raise ProcessingError(
                    "Pagina muito grande para o DPI solicitado. Reduza o parametro dpi.", 413
                )
            total_pixels += pixels
            if total_pixels > settings.max_total_megapixels * 1_000_000:
                raise ProcessingError(
                    "Limite total de pixels excedido. Reduza o DPI ou divida o PDF.", 413
                )
        for page in document:
            pixmap = page.get_pixmap(dpi=options.dpi, colorspace=pymupdf.csRGB, alpha=False)
            image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, 3
            ).copy()
            del pixmap
            if options.auto_orient:
                orientation = detect_orientation(image, report)
                if orientation:
                    image = rotate_quadrant(image, orientation)
                    report.reoriented_pages += 1
            gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
            angle = estimate_skew(gray, options.max_skew)
            del gray
            if angle:
                image = rotate_without_cropping(image, angle)
                report.deskewed_pages += 1
            enhanced = enhance_image(image, options)
            del image
            # PNG e sem perdas. DPI maior nao representa recuperacao de detalhe perdido.
            encoded_image = (
                cv2.cvtColor(enhanced, cv2.COLOR_RGB2BGR)
                if enhanced.ndim == 3 else enhanced
            )
            ok, png = cv2.imencode(".png", encoded_image, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            if not ok:
                raise ProcessingError("Nao foi possivel codificar uma pagina.", 500)
            height, width = enhanced.shape[:2]
            new_page = output.new_page(
                width=width * 72 / options.dpi, height=height * 72 / options.dpi
            )
            new_page.insert_image(new_page.rect, stream=png.tobytes())
            report.pages += 1
            del enhanced, encoded_image, png
        metadata = {
            key: value for key, value in document.metadata.items()
            if key in {"title", "author", "subject", "keywords", "creationDate"} and value
        }
        metadata["producer"] = "Endireita PDF - PyMuPDF / OpenCV"
        output.set_metadata(metadata)
        output.save(target, garbage=4, deflate=True)
    return report


def process_zip(source: Path, target: Path, options: Options, settings: Settings) -> Report:
    """Processa PDFs de um ZIP sem aceitar caminhos perigosos ou zip bombs.

    Arquivos que nao sao PDF sao copiados sem alteracao e a estrutura de
    diretorios do pacote e preservada.
    """
    report = Report()
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ProcessingError("ZIP invalido, corrompido ou nao suportado.") from exc
    with archive, tempfile.TemporaryDirectory(prefix="endireita-pdf-zip-") as workspace:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(entries) > settings.max_zip_files:
            raise ProcessingError(
                f"Limite de {settings.max_zip_files} arquivos no ZIP excedido.", 413
            )
        uncompressed = sum(entry.file_size for entry in entries)
        if uncompressed > settings.max_zip_uncompressed_mb * 1024 * 1024:
            raise ProcessingError("ZIP descompactado excede o limite permitido.", 413)
        base = Path(workspace).resolve()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for entry in archive.infolist():
                relative = Path(entry.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ProcessingError("ZIP contem um caminho de arquivo invalido.")
                if entry.is_dir():
                    output.writestr(entry, b"")
                    continue
                destination = (base / relative).resolve()
                if not destination.is_relative_to(base):
                    raise ProcessingError("ZIP contem um caminho de arquivo invalido.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as incoming, destination.open("wb") as extracted:
                    shutil.copyfileobj(incoming, extracted)
                if relative.suffix.lower() != ".pdf":
                    output.write(destination, arcname=entry.filename)
                    continue
                processed = destination.with_name(f"{destination.stem}.processed.pdf")
                pdf_report = process_pdf(destination, processed, options, settings)
                report.pages += pdf_report.pages
                report.deskewed_pages += pdf_report.deskewed_pages
                report.reoriented_pages += pdf_report.reoriented_pages
                report.warnings.update(pdf_report.warnings)
                output.write(processed, arcname=entry.filename)
    return report
