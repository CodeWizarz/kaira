from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from uuid import uuid4

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds

from kaira.schemas import align_table_to_schema

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParquetWriteConfig:
    compression: str = "zstd"
    compression_level: int | None = 3
    use_dictionary: bool = True
    write_statistics: bool = True
    max_rows_per_file: int = 2_000_000
    max_rows_per_group: int = 250_000
    min_rows_per_group: int = 50_000


class ParquetDatasetWriter:
    """
    Buffered writer for a hive-partitioned Parquet dataset.

    Design goals:
      - keep ingestion async-friendly (buffer in memory, flush in batches)
      - produce files large enough for scan efficiency (avoid the small-file trap)
      - schema-align incoming batches to keep the canonical dataset stable
    """

    def __init__(
        self,
        *,
        root_dir: Path,
        schema: pa.Schema,
        partition_cols: Sequence[str],
        sort_keys: Sequence[str] = ("ts", "strike", "right"),
        write_cfg: ParquetWriteConfig | None = None,
    ) -> None:
        self._root_dir = Path(root_dir)
        self._schema = schema
        self._partition_cols = list(partition_cols)
        self._sort_keys = list(sort_keys)
        self._write_cfg = write_cfg or ParquetWriteConfig()

        self._buffer: list[pa.Table] = []
        self._buffer_rows = 0

        self._root_dir.mkdir(parents=True, exist_ok=True)

    @property
    def buffered_rows(self) -> int:
        return self._buffer_rows

    def append(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        aligned = align_table_to_schema(table, self._schema)
        self._buffer.append(aligned)
        self._buffer_rows += aligned.num_rows

    def append_many(self, tables: Iterable[pa.Table]) -> None:
        for t in tables:
            self.append(t)

    def flush(self) -> int:
        if not self._buffer:
            return 0

        table = pa.concat_tables(self._buffer, promote_options="default")
        self._buffer.clear()
        self._buffer_rows = 0

        table = self._maybe_sort(table)

        file_format = ds.ParquetFileFormat()
        file_options = file_format.make_write_options(
            compression=self._write_cfg.compression,
            compression_level=self._write_cfg.compression_level,
            use_dictionary=self._write_cfg.use_dictionary,
            write_statistics=self._write_cfg.write_statistics,
        )

        written: list[str] = []

        def _visitor(written_file: ds.WrittenFile) -> None:
            written.append(str(written_file.path))

        ds.write_dataset(
            table,
            base_dir=str(self._root_dir),
            format=file_format,
            file_options=file_options,
            partitioning=self._partition_cols,
            partitioning_flavor="hive",
            existing_data_behavior="overwrite_or_ignore",
            basename_template=f"part-{uuid4().hex}-{{i}}.parquet",
            max_rows_per_file=self._write_cfg.max_rows_per_file,
            max_rows_per_group=self._write_cfg.max_rows_per_group,
            min_rows_per_group=self._write_cfg.min_rows_per_group,
            file_visitor=_visitor,
        )

        if written:
            log.info("Wrote %d parquet files", len(written))
            log.debug("Files: %s", written[:5])
        return table.num_rows

    def _maybe_sort(self, table: pa.Table) -> pa.Table:
        if not self._sort_keys:
            return table
        missing = [k for k in self._sort_keys if k not in table.schema.names]
        if missing:
            return table

        sort_keys = [(k, "ascending") for k in self._sort_keys]
        try:
            idx = pc.sort_indices(table, sort_keys=sort_keys)
            return table.take(idx)
        except Exception:
            log.exception("Sort failed; writing unsorted batch")
            return table
