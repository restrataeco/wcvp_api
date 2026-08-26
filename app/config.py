import os
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Path to the data file. Just replace this file and hit /reload — no re-import needed.
    csv_path: str = "data/wcvp_names.csv"

    # CSV files sometimes use ; or | as a separator depending on locale/export settings.
    csv_delimiter: str = ","

    # Default page size for list/search endpoints, and the max a caller can request.
    default_page_size: int = 20
    max_page_size: int = 500

    # Simple shared-secret token to protect the /reload endpoint in production.
    # Set a real value in your .env file. Leave blank to disable auth (fine for local testing only).
    reload_token: str = ""

    # Comma-separated list of column names to search across when no explicit
    # columns are passed to /search. Leave blank to auto-search all text columns.
    default_search_columns: str = ""

    csv_source_url: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
