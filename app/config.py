import os
from dataclasses import dataclass


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} precisa ser maior que zero")
    return value


@dataclass(frozen=True)
class Settings:
    max_upload_mb: int = 50
    max_pages: int = 100
    max_page_megapixels: int = 20
    max_total_megapixels: int = 300
    max_concurrent_jobs: int = 2
    queue_timeout_seconds: int = 5
    max_zip_files: int = 200
    max_zip_uncompressed_mb: int = 500

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            max_upload_mb=positive_int("MAX_UPLOAD_MB", 50),
            max_pages=positive_int("MAX_PAGES", 100),
            max_page_megapixels=positive_int("MAX_PAGE_MEGAPIXELS", 20),
            max_total_megapixels=positive_int("MAX_TOTAL_MEGAPIXELS", 300),
            max_concurrent_jobs=positive_int("MAX_CONCURRENT_JOBS", 2),
            queue_timeout_seconds=positive_int("QUEUE_TIMEOUT_SECONDS", 5),
            max_zip_files=positive_int("MAX_ZIP_FILES", 200),
            max_zip_uncompressed_mb=positive_int("MAX_ZIP_UNCOMPRESSED_MB", 500),
        )
