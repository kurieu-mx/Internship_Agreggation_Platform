"""Google Sheets sink.

Authentication uses a service account, which is what a scheduled, headless job
needs - the original OAuth "open a browser and click consent" flow cannot run
from cron. Share the target spreadsheet with the service account's email
address and it can write to it.

The credentials file is never committed; its path comes from
GOOGLE_CREDENTIALS_FILE. See README for setup.
"""

import logging
import os
from typing import Dict, List, Set

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
from models import Job

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsError(RuntimeError):
    pass


class SheetsWriter:
    def __init__(self, spreadsheet_id: str = None, credentials_file: str = None):
        self.spreadsheet_id = spreadsheet_id or config.SPREADSHEET_ID
        self.credentials_file = credentials_file or config.GOOGLE_CREDENTIALS_FILE
        self.sheet_name = config.SHEET_NAME
        self.service = None

    def connect(self):
        if not os.path.exists(self.credentials_file):
            raise SheetsError(
                f"credentials file '{self.credentials_file}' not found. "
                "Create a service account key in Google Cloud Console and point "
                "GOOGLE_CREDENTIALS_FILE at it (see README)."
            )
        if not self.spreadsheet_id:
            raise SheetsError("SPREADSHEET_ID is not set.")

        credentials = service_account.Credentials.from_service_account_file(
            self.credentials_file, scopes=SCOPES
        )
        self.service = build("sheets", "v4", credentials=credentials)
        log.info("connected to spreadsheet %s", self.spreadsheet_id)
        return self

    def _values(self):
        if self.service is None:
            raise SheetsError("connect() must be called first")
        return self.service.spreadsheets().values()

    def ensure_headers(self):
        """Write the header row if the sheet is empty."""
        try:
            existing = self._values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A1:Z1",
            ).execute()
            if existing.get("values"):
                return
            self._values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A1",
                valueInputOption="RAW",
                body={"values": [config.COLUMN_HEADERS]},
            ).execute()
            log.info("wrote header row")
        except HttpError as exc:
            raise SheetsError(f"could not write headers: {exc}") from exc

    def existing_keys(self) -> Set[str]:
        """Company+title keys already in the sheet, for incremental appends."""
        try:
            result = self._values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A2:B",
            ).execute()
        except HttpError as exc:
            raise SheetsError(f"could not read existing rows: {exc}") from exc

        keys = set()
        for row in result.get("values", []):
            if len(row) >= 2:
                keys.add(f"{row[0].strip().lower()}::{row[1].strip().lower()}")
        log.info("sheet already contains %d rows", len(keys))
        return keys

    def append(self, jobs: List[Job]) -> int:
        """Append only jobs not already present. Returns the number written."""
        if not jobs:
            return 0

        self.ensure_headers()
        known = self.existing_keys()
        new_jobs = [job for job in jobs if job.key not in known]
        if not new_jobs:
            log.info("no new jobs to append")
            return 0

        try:
            self._values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.sheet_name}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [job.to_row() for job in new_jobs]},
            ).execute()
        except HttpError as exc:
            raise SheetsError(f"could not append rows: {exc}") from exc

        log.info("appended %d new jobs", len(new_jobs))
        return len(new_jobs)

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"


def summarize(jobs: List[Job]) -> Dict[str, int]:
    """Counts by field category, for the run summary."""
    counts: Dict[str, int] = {}
    for job in jobs:
        counts[job.field_category] = counts.get(job.field_category, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
