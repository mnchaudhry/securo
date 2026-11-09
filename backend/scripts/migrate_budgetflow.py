#!/usr/bin/env python
"""One-shot importer: BudgetFlow (Supabase Postgres export) -> Securo.

Reads the JSON exports produced from BudgetFlow's database and writes them
into a Securo database using the application's own models.

Mapping
-------
* BudgetFlow ``users``        -> Securo ``users`` (+ personal workspace + owner membership)
  - ``passwordHash`` is copied verbatim into ``hashed_password``. It is a
    standard bcrypt ``$2a$12$`` hash, which Securo's pwdlib helper verifies
    and transparently re-hashes to argon2id on first login.
* BudgetFlow ``buckets``      -> Securo ``categories`` (name / color / icon preserved)
* BudgetFlow ``transactions`` -> Securo ``transactions`` on a single imported account
  - ``amoun``t is always positive; direction is carried by ``type`` (credit/debit).
  - ``INCOME`` -> ``credit``, ``EXPENSE`` -> ``debit``.

The import is idempotent:
* users are matched by (lowercased) email;
* categories by (workspace, name);
* transactions by ``external_id`` (the BudgetFlow ``_id``).

Usage (inside the backend image / venv, with DATABASE_URL pointing at Securo):
    python scripts/migrate_budgetflow.py --input /data --admin-email you@example.com
Add ``--dry-run`` to report what would happen without writing anything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services.workspace_service import create_personal_workspace_for_user

# BudgetFlow renders Material icon names; Securo uses lucide names.
ICON_MAP = {
    "account_balance_wallet": "wallet",
    "account_balance": "landmark",
    "volunteer_activism": "heart-handshake",
    "savings": "piggy-bank",
    "credit_card": "credit-card",
    "restaurant": "utensils",
    "shopping_cart": "shopping-cart",
    "home": "house",
    "directions_car": "car",
    "local_hospital": "hospital",
    "school": "graduation-cap",
    "flight": "plane",
    "payments": "banknote",
    "receipt_long": "receipt",
}
DEFAULT_ICON = "tag"
DEFAULT_COLOR = "#6B7280"
DEFAULT_ACCOUNT_NAME = "Imported"
DEFAULT_CURRENCY = "PKR"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load(input_dir: Path, name: str) -> list[dict[str, Any]]:
    path = input_dir / name
    if not path.exists():
        _log(f"  ! missing {path}, treating as empty")
        return []
    data = json.loads(path.read_text())
    return list(data or [])


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    # "2026-09-12T00:00:00" or "2026-09-12"
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _preferences(display_name: str | None) -> dict[str, Any]:
    prefs: dict[str, Any] = {
        "language": "en",
        "date_format": "DD/MM/YYYY",
        "currency_display": DEFAULT_CURRENCY,
    }
    if display_name:
        prefs["display_name"] = display_name
    return prefs


async def _get_or_create_category(session, workspace_id, user_id, bucket) -> Category:
    name = (bucket.get("name") or "Uncategorized").strip()
    existing = await session.execute(
        select(Category).where(
            Category.workspace_id == workspace_id, Category.name == name
        )
    )
    found = existing.scalar_one_or_none()
    if found:
        return found
    color = (bucket.get("color") or DEFAULT_COLOR).strip() or DEFAULT_COLOR
    category = Category(
        user_id=user_id,
        workspace_id=workspace_id,
        name=name[:100],
        icon=ICON_MAP.get((bucket.get("icon") or "").strip(), DEFAULT_ICON),
        color=color[:7],
        is_system=False,
        is_hidden=False,
    )
    session.add(category)
    await session.flush()
    return category


async def run(input_dir: Path, admin_email: str | None, dry_run: bool) -> int:
    users = _load(input_dir, "users.json")
    buckets = _load(input_dir, "buckets.json")
    transactions = _load(input_dir, "transactions.json")

    _log(f"Source: {len(users)} users, {len(buckets)} buckets, {len(transactions)} transactions")
    if dry_run:
        _log("DRY RUN — no changes will be written")

    stats = {"users": 0, "users_skipped": 0, "workspaces": 0,
             "accounts": 0, "categories": 0, "transactions": 0, "transactions_skipped": 0}

    admin_email = (admin_email or "").strip().lower() or None

    async with async_session_maker() as session:
        user_map: dict[str, User] = {}
        for src in users:
            email = (src.get("email") or "").strip().lower()
            if not email:
                continue
            found = await session.execute(select(User).where(User.email == email))
            user = found.scalar_one_or_none()
            if user is not None:
                stats["users_skipped"] += 1
                _log(f"  = user exists: {email}")
            else:
                user = User(
                    email=email,
                    hashed_password=src.get("passwordHash") or "",
                    is_active=True,
                    is_verified=True,
                    is_superuser=(admin_email is not None and email == admin_email),
                    preferences=_preferences(src.get("name")),
                )
                session.add(user)
                await session.flush()
                stats["users"] += 1
                _log(f"  + user: {email}" + (" (superuser)" if user.is_superuser else ""))
            user_map[src["id"]] = user

        # Default workspace + owner membership per user.
        workspace_map: dict[str, Workspace] = {}
        for src_id, user in user_map.items():
            workspace = await create_personal_workspace_for_user(session, user)
            workspace_map[src_id] = workspace
            stats["workspaces"] += 1

        # One manual account per user to hold the imported ledger.
        account_map: dict[str, Account] = {}
        for src_id, user in user_map.items():
            workspace = workspace_map[src_id]
            found = await session.execute(
                select(Account).where(
                    Account.workspace_id == workspace.id,
                    Account.name == DEFAULT_ACCOUNT_NAME,
                )
            )
            account = found.scalar_one_or_none()
            if account is None:
                account = Account(
                    user_id=user.id,
                    workspace_id=workspace.id,
                    name=DEFAULT_ACCOUNT_NAME,
                    type="checking",
                    currency=DEFAULT_CURRENCY,
                    balance=Decimal("0.00"),
                )
                session.add(account)
                await session.flush()
                stats["accounts"] += 1
                _log(f"  + account: {DEFAULT_ACCOUNT_NAME} (for {user.email})")
            account_map[src_id] = account

        # Buckets -> categories, keyed by BudgetFlow bucket id.
        category_map: dict[str, Category] = {}
        for bucket in buckets:
            owner = user_map.get(bucket.get("userId"))
            if owner is None:
                continue
            workspace = workspace_map[bucket["userId"]]
            category = await _get_or_create_category(
                session, workspace.id, owner.id, bucket
            )
            category_map[bucket["id"]] = category
            stats["categories"] += 1

        # Transactions -> Securo transactions.
        for src in transactions:
            owner = user_map.get(src.get("userId"))
            if owner is None:
                continue
            external_id = str(src.get("id"))
            dup = await session.execute(
                select(Transaction.id).where(Transaction.external_id == external_id)
            )
            if dup.scalar_one_or_none() is not None:
                stats["transactions_skipped"] += 1
                continue
            workspace = workspace_map[src["userId"]]
            account = account_map[src["userId"]]
            category = category_map.get(src.get("bucketId") or "")
            amount_abs = abs(_as_decimal(src.get("amount", 0)))
            tx_type = "credit" if (src.get("type") == "INCOME") else "debit"
            tx_date = _parse_date(src.get("date"))
            note = (src.get("note") or "").strip() or "(no description)"
            session.add(
                Transaction(
                    user_id=owner.id,
                    workspace_id=workspace.id,
                    account_id=account.id,
                    category_id=category.id if category else None,
                    external_id=external_id,
                    description=note[:500],
                    amount=amount_abs,
                    currency=DEFAULT_CURRENCY,
                    date=tx_date,
                    effective_date=tx_date,
                    type=tx_type,
                    source="import",
                    status="posted",
                    raw_data=src,
                )
            )
            stats["transactions"] += 1

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    _log("")
    _log("Result:")
    for key, value in stats.items():
        _log(f"  {key}: {value}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import BudgetFlow data into Securo.")
    parser.add_argument("--input", required=True, type=Path,
                        help="Directory containing users.json, buckets.json, transactions.json")
    parser.add_argument("--admin-email", default=None,
                        help="Email to mark as is_superuser (bootstrap admin)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report without writing")
    args = parser.parse_args()

    if not args.input.is_dir():
        _log(f"Input directory not found: {args.input}")
        return 2
    return asyncio.run(run(args.input, args.admin_email, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
