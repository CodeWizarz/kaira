from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class DataPaths(BaseModel):
    root: Path = Field(default_factory=lambda: Path("data"))

    @property
    def bronze(self) -> Path:
        return self.root / "bronze"

    @property
    def silver(self) -> Path:
        return self.root / "silver"

    @property
    def quarantine(self) -> Path:
        return self.root / "quarantine"


class AppConfig(BaseModel):
    data: DataPaths = Field(default_factory=DataPaths)

