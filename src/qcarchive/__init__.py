"""qc-columnar-archive 离线查询引擎（仅支持 format.md 所述简化列档）。"""

from .errors import ArchiveError, ProjectionPageError
from .models import Hit, PageRef, QueryReport, ReadRecord, Structure
from .reader import GatedPageReader, query_archive
from .builder import ArchiveBuilder
from .compare import compare_archives, write_comparison_report
from .importer import (
    ArchiveImportError,
    import_archive,
    import_directory,
    import_jsonl_archive,
    import_jsonl_directory,
)
from .vault import (
    Vault,
    VaultCorruptError,
    VaultError,
    forget_version,
    restore_version,
    save_version,
)

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
    "ArchiveImportError",
    "import_jsonl_archive",
    "import_jsonl_directory",
    "import_archive",
    "import_directory",
    "compare_archives",
    "write_comparison_report",
    "Vault",
    "VaultError",
    "VaultCorruptError",
    "save_version",
    "restore_version",
    "forget_version",
]
