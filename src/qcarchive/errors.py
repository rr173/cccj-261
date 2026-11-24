"""异常类型。"""


class ArchiveError(Exception):
    """档案本身无法解释或查询必需的结构/谓词数据不可用。

    结构文件、清单（manifest）、谓词列页出现缺失、损坏或语义不一致时抛出。
    这类错误意味着查询无法保证正确性，整体按档案错误处理，不交付命中。
    """


class ProjectionPageError(Exception):
    """投影载荷页（检查明细 / 部件备注）缺失或损坏。

    与 ArchiveError 不同：投影页只影响引用该页的命中，
    受影响命中标记为不可交付，其他命中仍可正常交付。
    """

    def __init__(self, message: str, *, page_id: str | None = None,
                 path: str | None = None, reason: str | None = None):
        super().__init__(message)
        self.page_id = page_id
        self.path = path
        self.reason = reason
