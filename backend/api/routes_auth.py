"""Auth (register company, login, refresh) and admin endpoints (users, company settings, usage)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import Services, require, services
from backend.api.schemas import (
    CompanyOut,
    CompanySettingsIn,
    LoginIn,
    RefreshIn,
    RegisterCompanyIn,
    TokenOut,
    UserIn,
    UserOut,
)
from backend.db import TenantRepo
from backend.models import Company, RefreshToken, ScreeningRun, UsageEvent, User
from backend.security import (
    Principal,
    hash_password,
    hash_refresh_token,
    issue_access_token,
    new_refresh_token,
    verify_password,
)
from backend.services.common import audit
from backend.services.privacy import retention_status

router = APIRouter(prefix="/v1", tags=["auth"])
_INVALID_LOGIN = "Invalid email or password"


async def _issue_tokens(session: AsyncSession, svc: Services, user: User) -> TokenOut:
    principal = Principal(user.id, user.company_id, user.role)  # type: ignore[arg-type]
    access, ttl = issue_access_token(principal, svc.settings)
    refresh, refresh_hash = new_refresh_token()
    session.add(
        RefreshToken(
            company_id=user.company_id,
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=datetime.now(UTC) + timedelta(seconds=svc.settings.refresh_token_ttl_s),
        )
    )
    return TokenOut(
        access_token=access,
        expires_in=ttl,
        refresh_token=refresh,
        company_id=user.company_id,
        user_id=user.id,
        role=user.role,
    )


@router.post("/auth/register-company", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register_company(
    body: RegisterCompanyIn,
    svc: Annotated[Services, Depends(services)],
) -> TokenOut:
    email = body.admin_email.lower()
    async with svc.db.system_session() as s:  # no tenant exists yet
        if (await s.execute(select(User.id).where(User.email == email))).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
        company = Company(id=uuid.uuid4(), name=body.company_name)
        s.add(company)
        await s.flush()
        user = User(
            id=uuid.uuid4(),
            company_id=company.id,
            email=email,
            password_hash=hash_password(body.admin_password),
            role="admin",
        )
        s.add(user)
        await s.flush()
        audit(
            TenantRepo(s, company.id),
            Principal(user.id, company.id, "admin"),
            "company.registered",
            "company",
            company.id,
            after={"name": company.name},
        )
        return await _issue_tokens(s, svc, user)


@router.post("/auth/login", response_model=TokenOut)
async def login(
    body: LoginIn,
    svc: Annotated[Services, Depends(services)],
) -> TokenOut:
    async with svc.db.system_session() as s:  # the tenant is not known until the user is found
        user = (await s.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
        if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID_LOGIN)
        return await _issue_tokens(s, svc, user)


@router.post("/auth/refresh", response_model=TokenOut)
async def refresh(
    body: RefreshIn,
    svc: Annotated[Services, Depends(services)],
) -> TokenOut:
    """Rotate: the presented refresh token is revoked and a new pair issued. Reusing a revoked token revokes
    every token of that user (stolen-token detection)."""
    now = datetime.now(UTC)
    result = None
    async with svc.db.system_session() as s:
        token = (
            await s.execute(
                select(RefreshToken)
                .where(RefreshToken.token_hash == hash_refresh_token(body.refresh_token))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if token is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
        if token.revoked_at is not None:
            await s.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == token.user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        else:
            if token.expires_at <= now:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired")
            user = (await s.execute(select(User).where(User.id == token.user_id))).scalar_one()
            if not user.is_active:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
            token.revoked_at = now
            result = await _issue_tokens(s, svc, user)
    # Raise only after committing the revocation; raising inside the transaction would roll it back.
    if result is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token reuse detected; sign in again")
    return result


@router.get("/users", response_model=list[UserOut])
async def list_users(
    principal: Annotated[Principal, Depends(require("users:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> list[User]:
    async with svc.tenant(principal) as repo:
        return await repo.list(User, order_by=[User.created_at])


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserIn,
    principal: Annotated[Principal, Depends(require("users:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> User:
    email = body.email.lower()
    async with svc.db.system_session() as s:  # email uniqueness is global (it is the login identity)
        if (await s.execute(select(User.id).where(User.email == email))).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    async with svc.tenant(principal) as repo:
        user = repo.add(User(id=uuid.uuid4(), email=email, password_hash=hash_password(body.password), role=body.role))
        await repo.flush()
        audit(repo, principal, "user.created", "user", user.id, after={"role": body.role})
        return user


async def _company_out(repo: TenantRepo, svc: Services, company: Company) -> CompanyOut:
    return CompanyOut(
        id=company.id,
        name=company.name,
        retention_days=company.retention_days,
        retention_sweep_hour_utc=svc.settings.retention_sweep_hour_utc,
        **await retention_status(repo, company.retention_days),
    )


@router.get("/company", response_model=CompanyOut)
async def get_company(
    principal: Annotated[Principal, Depends(require("company:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> CompanyOut:
    """Company settings for the dashboard: retention period, daily sweep hour and how many candidates it affects."""
    async with svc.tenant(principal) as repo:
        company = (await repo.session.execute(select(Company).where(Company.id == principal.company_id))).scalar_one()
        return await _company_out(repo, svc, company)


@router.patch("/company", response_model=CompanyOut)
async def update_company(
    body: CompanySettingsIn,
    principal: Annotated[Principal, Depends(require("company:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> CompanyOut:
    """Change the retention period. Candidates past the new period are deleted by the next daily sweep."""
    async with svc.tenant(principal) as repo:
        company = (
            await repo.session.execute(select(Company).where(Company.id == principal.company_id).with_for_update())
        ).scalar_one()
        before = {"retention_days": company.retention_days}
        company.retention_days = body.retention_days
        audit(repo, principal, "company.updated", "company", company.id, before=before, after=body.model_dump())
        await repo.flush()
        return await _company_out(repo, svc, company)


def _as_utc(value: datetime | None) -> datetime | None:
    """Timestamps without a timezone are interpreted as UTC."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


@router.get("/usage")
async def usage(
    principal: Annotated[Principal, Depends(require("usage:read"))],
    svc: Annotated[Services, Depends(services)],
    from_: Annotated[
        datetime | None, Query(alias="from", description="Inclusive start (ISO 8601; UTC if no zone)")
    ] = None,
    to: Annotated[datetime | None, Query(description="Exclusive end (ISO 8601; UTC if no zone)")] = None,
) -> dict[str, Any]:
    """Tokens, OCR pages and cost for the company: totals, by kind, per screening run and per UTC day."""
    from_, to = _as_utc(from_), _as_utc(to)
    if from_ is not None and to is not None and from_ >= to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "'from' must be earlier than 'to'")
    async with svc.tenant(principal) as repo:
        where = [UsageEvent.company_id == principal.company_id]
        if from_ is not None:
            where.append(UsageEvent.created_at >= from_)
        if to is not None:
            where.append(UsageEvent.created_at < to)
        sums = (
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.ocr_pages), 0),
            func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
        )
        by_kind_rows = (
            await repo.session.execute(select(UsageEvent.kind, *sums).where(*where).group_by(UsageEvent.kind))
        ).all()
        run_rows = (
            await repo.session.execute(
                select(UsageEvent.run_id, ScreeningRun.job_id, *sums)
                .join(ScreeningRun, ScreeningRun.id == UsageEvent.run_id)
                .where(*where, ScreeningRun.company_id == principal.company_id)
                .group_by(UsageEvent.run_id, ScreeningRun.job_id)
            )
        ).all()
        day = func.date(func.timezone("UTC", UsageEvent.created_at))
        day_rows = (await repo.session.execute(select(day, *sums).where(*where).group_by(day).order_by(day))).all()

    def row(values: Any) -> dict[str, float]:
        pt, ct, pages, cost = values
        return {
            "prompt_tokens": int(pt),
            "completion_tokens": int(ct),
            "ocr_pages": int(pages),
            "cost_usd": round(float(cost), 6),
        }

    by_kind = {kind: row(rest) for kind, *rest in by_kind_rows}
    totals = {
        k: sum(v[k] for v in by_kind.values()) for k in ("prompt_tokens", "completion_tokens", "ocr_pages", "cost_usd")
    }
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return {
        "from": from_,
        "to": to,
        "totals": totals,
        "by_kind": by_kind,
        "runs": [{"run_id": str(run_id), "job_id": str(job_id), **row(rest)} for run_id, job_id, *rest in run_rows],
        "by_day": [{"date": d.isoformat(), **row(rest)} for d, *rest in day_rows],
    }
