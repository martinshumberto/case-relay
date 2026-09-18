"""Authentication context.

The `X-Auth: <company_id>:<role>` scheme is deliberately left unsigned: the
brief declares it a simplification. This module only validates the header's
shape; authorization itself lives in the routes.
"""

from fastapi import Depends, Header, HTTPException

ROLES = frozenset({"user", "admin"})


def current_ctx(x_auth: str | None = Header(default=None)) -> dict:
    if not x_auth:
        raise HTTPException(401, "X-Auth ausente")

    parts = x_auth.split(":")
    if len(parts) != 2:
        raise HTTPException(401, "X-Auth malformado")

    raw_company, role = parts

    if role not in ROLES:
        raise HTTPException(401, "papel desconhecido")

    # Malformed input must surface as 401, never as a 5xx.
    try:
        company_id = int(raw_company)
    except ValueError:
        raise HTTPException(401, "X-Auth malformado")

    if company_id <= 0:
        raise HTTPException(401, "X-Auth malformado")

    return {"company_id": company_id, "role": role}


def require_admin(ctx: dict = Depends(current_ctx)) -> dict:
    """Server-side role guard: the frontend decides what to render, not what is
    permitted."""
    if ctx["role"] != "admin":
        raise HTTPException(403, "requer papel admin")
    return ctx
