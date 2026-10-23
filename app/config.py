from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Where the CSVs extracted from Kew's zip are written and read.
    csv_path: str = "data/wcvp_names.csv"

    db_path: str = "data/wcvp.db"
    build_chunk_size: int = 50000
    index_columns: str = ""
    force_download: bool = False
    force_rebuild: bool = False

    # Kew's WCVP CSVs are pipe-delimited.
    csv_delimiter: str = "|"

    # Simple shared-secret token to protect /reload and /check-update in production.
    # Set a real value in your .env file. Leave blank to disable auth (fine for local testing only).
    reload_token: str = ""

    # Distribution data (wcvp_distribution.csv), joined to names on plant_name_id.
    distribution_csv_path: str = "data/wcvp_distribution.csv"

    # Kew publishes both CSVs in one zip, updated every 4–6 months. The app
    # checks it every UPDATE_CHECK_HOURS (one small HEAD request) and only
    # downloads, extracts and rebuilds when Kew's Last-Modified/ETag changes.
    wcvp_zip_url: str = "https://sftp.kew.org/pub/data-repositories/WCVP/wcvp.zip"
    update_check_enabled: bool = True
    update_check_hours: float = 24
    # Remembers which version of the zip the current CSVs came from.
    source_state_path: str = "data/wcvp_source.json"

    # extra="ignore": keys this app no longer uses (e.g. CSV_SOURCE_URL,
    # DEFAULT_PAGE_SIZE in an older .env) are skipped instead of failing startup.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
