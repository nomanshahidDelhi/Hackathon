from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    project: str
    location: str

    def table(self, dataset_table: str) -> str:
        return f"`{self.project}.{dataset_table}`"


def load_settings() -> Settings:
    env = os.environ
    project = env.get("GCP_PROJECT_ID") or env.get("GOOGLE_CLOUD_PROJECT")
    location = env.get("BQ_LOCATION") or env.get("GCP_REGION") or env.get("GOOGLE_CLOUD_REGION")
    if not project or not location:
        raise RuntimeError("set GCP_PROJECT_ID and GCP_REGION")
    return Settings(project=project, location=location)
