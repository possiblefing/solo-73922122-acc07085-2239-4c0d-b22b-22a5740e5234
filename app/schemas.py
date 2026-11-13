"""API 请求/响应模型（用于 /docs 文档与校验）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LockRequest(BaseModel):
    left_id: str = Field(..., description="银行流水侧行 ID")
    right_id: str = Field(..., description="收款系统侧行 ID")
    expected_revision: int = Field(..., ge=1, description="客户端所见修订号，过期将被拒绝")


class UnlockRequest(BaseModel):
    left_id: str = Field(..., description="要撤销锁定的银行流水侧行 ID")
    expected_revision: int = Field(..., ge=1, description="客户端所见修订号，过期将被拒绝")


class RowOut(BaseModel):
    id: str
    date: str
    currency: str
    amount_cents: int


class PairOut(BaseModel):
    left_id: str
    right_id: str


class ConfirmedPairOut(BaseModel):
    left_id: str
    right_id: str
    source: str = Field(..., description="locked=人工锁定；all_optimal_solutions=所有最优方案共有")


class AnalysisOut(BaseModel):
    max_matching_size: int = Field(..., description="整批最大配对数")
    locked_pair_count: int
    confirmed_pairs: list[ConfirmedPairOut] = Field(
        ..., description="确定配对：人工锁定 + 所有最优方案共有的配对"
    )
    possible_pairs_by_bank_row: dict[str, list[str]] = Field(
        ..., description="其余每个银行行可参与最优方案的候选配对"
    )
    possible_pairs_by_system_row: dict[str, list[str]] = Field(
        ..., description="其余每个系统行可参与最优方案的候选配对"
    )
    necessarily_unmatched_bank_rows: list[str] = Field(
        ..., description="任何最优方案都匹配不到的银行行"
    )
    necessarily_unmatched_system_rows: list[str] = Field(
        ..., description="任何最优方案都匹配不到的系统行"
    )


class BatchState(BaseModel):
    batch_id: str
    revision: int = Field(..., description="当前修订号，每次锁定/撤销递增")
    created_at: str
    bank_rows: list[RowOut]
    system_rows: list[RowOut]
    locks: list[PairOut]
    analysis: AnalysisOut


class BatchSummary(BaseModel):
    batch_id: str
    revision: int
    max_matching_size: int
    bank_count: int
    system_count: int
    created_at: str
