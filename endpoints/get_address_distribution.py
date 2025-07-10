# encoding: utf-8
import time
from typing import List

from fastapi import Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.future import select
from starlette.responses import Response

from constants import ADDRESS_RANKINGS
from dbsession import async_session_blocks
from endpoints import sql_db_only
from models.DistributionTier import DistributionTier as DbDistributionTier
from server import app


class DistributionTier(BaseModel):
    timestamp: int
    tier: int
    count: int
    amount: int


@app.get(
    "/addresses/distribution",
    response_model=List[DistributionTier],
    tags=["Kaspa addresses"],
    summary="Get Kaspa address distribution by balance tier",
    description="Get address distribution tiers, use 'before' to get historical data (must be aligned to a full hour).\n\n"
    "Addresses are grouped by their balance (KAS) in powers of ten. Tier 0:[0.0001..1), 1:[1..10), ..., 10:[1b..10b).",
    openapi_extra={"strict_query_params": True},
)
@sql_db_only
async def get_distribution_tiers(response: Response, before: int | None = Query(None)):
    if not ADDRESS_RANKINGS:
        raise HTTPException(status_code=503, detail="Distribution tiers is disabled")

    response.headers["Cache-Control"] = "public, max-age=180"
    if before is not None:
        if before % 3_600_000 != 0:
            raise HTTPException(status_code=400, detail="'before' must be aligned to a full hour")
        now_ms = int(time.time() * 1000)
        if before < now_ms - 3_600_000:
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif before > now_ms + 600_000:
            return []

    async with async_session_blocks() as s:
        ts_subquery = select(func.max(DbDistributionTier.timestamp))
        if before is not None:
            ts_subquery = ts_subquery.where(DbDistributionTier.timestamp < before)
        distribution_tiers = (
            (
                await s.execute(
                    select(DbDistributionTier)
                    .where(DbDistributionTier.timestamp == ts_subquery.scalar_subquery())
                    .order_by(DbDistributionTier.tier)
                )
            )
            .scalars()
            .all()
        )

    return [
        DistributionTier(timestamp=dt.timestamp, tier=dt.tier, count=dt.count, amount=dt.amount)
        for dt in distribution_tiers
    ]
