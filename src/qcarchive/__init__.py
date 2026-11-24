"""qc-columnar-archive 离线查询引擎（仅支持 format.md 所述简化列档）。"""

from .errors import ArchiveError, ProjectionPageError
from .models import Hit, PageRef, QueryReport, ReadRecord, Structure
from .reader import GatedPageReader, query_archive
from .builder import ArchiveBuilder

__all__ = [
    "ArchiveError",
    "ProjectionPageError",
    "Hit",
    "PageRef",
    "QueryReport",
    "ReadRecord",
    "Structure",
    "GatedPageReader",
    "query_archive",
    "ArchiveBuilder",
]
