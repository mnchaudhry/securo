#!/usr/bin/env python
"""One-shot importer: BudgetFlow (Supabase Postgres export) -> Securo.

Reads the JSON exports produced from BudgetFlow's database and writes them
into a Securo database using the application's own models.

Mapping
-------
* BudgetFlow ``users``  -> Securo ``users`` (+ personal workspace + owner membership)
  - ``passwordHash`` is copied verbatim into ``hashed_password``. It is a
    standard bcrypt ``$2a$12$`` hash, which Securo's pwdlib helper verifies
    and transparently re-hashes to argon2id on first login.
* BudgetFlow ``buckets`` -> Securo ``accounts`` (default) or ``categories``.
  - ``accounts``  (bucket = account): one account per bucket, so each bucket's
    running balance is preserved as an account balance. Transactions land on
    their bucket's account.
  - ``categories`` (bucket = category): one category per bucket, plus a single
    "Imported" account holding every transaction.
* BudgetFlow ``transactions`` -> Securo ``transactions``
  - ``amount`` is always positive; direction is carried by ``type``
    (``INCOME`` -> ``credit``, ``EXPENSE`` -> ``debit``).

The import is idempotent: users by (lowercased) email, accounts/categories by
(workspace, name), transactions by ``external_id`` (the BudgetFlow ``_id``).

Usage (inside the backend image / venv, DATABASE_URL pointing at Securo):
    python scripts/migrate_budgetflow.py --input /data [--buckets-as accounts]
        [--admin-email you@example.com] [--dry-run]
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
from app.models.workspace import Workspace
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
IMPORTED_ACCOUNT_NAME = "Imported"     # categories mode only
FALLBACK_ACCOUNT_NAME = "Unassigned"   # a transaction with no bucket
DEFAULT_ACCOUNT_TYPE = "checking"
DEFAULT_CURRENCY = "PKR"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load(input_dir: Path, name: str) -> list[dict[str, Any]]:
    path = input_dir / name
    if not path.exists():
        _log(f"  ! missing {path}, treating as empty")
        return []
    return list(json.loads(path.read_text()) or [])


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


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


async def _get_or_create_account(session, workspace_id, user_id, name, type_=DEFAULT_ACCOUNT_TYPE) -> Account:
    found = await session.execute(
        select(Account).where(Account.workspace_id == workspace_id, Account.name == name)
    )
    account = found.scalar_one_or_none()
    if account:
        return account
    account = Account(
        user_id=user_id,
        workspace_id=workspace_id,
        name=name[:255],
        type=type_,
        currency=DEFAULT_CURRENCY,
        balance=Decimal("0.00"),
    )
    session.add(account)
    await session.flush()
    return account


async def _get_or_create_category(session, workspace_id, user_id, bucket) -> Category:
    name = (bucket.get("name") or "Uncategorized").strip()
    found = await session.execute(
        select(Category).where(Category.workspace_id == workspace_id, Category.name == name)
    )
    category = found.scalar_one_or_none()
    if category:
        return category
    category = Category(
        user_id=user_id,
        workspace_id=workspace_id,
        name=name[:100],
        icon=ICON_MAP.get((bucket.get("icon") or "").strip(), DEFAULT_ICON),
        color=((bucket.get("color") or DEFAULT_COLOR).strip() or DEFAULT_COLOR)[:7],
        is_system=False,
        is_hidden=False,
    )
    session.add(category)
    await session.flush()
    return category


async def run(input_dir: Path, admin_email: str | None, dry_run: bool, buckets_as: str) -> int:
    users = _load(input_dir, "users.json")
    buckets = _load(input_dir, "buckets.json")
    transactions = _load(input_dir, "transactions.json")

    _log(f"Source: {len(users)} users, {len(buckets)} buckets, {len(transactions)} transactions")
    _log(f"Buckets map to: {buckets_as}")
    if dry_run:
        _log("DRY RUN — no changes will be written")

    stats = {"users": 0, "users_skipped": 0, "accounts": 0, "categories": 0,
             "transactions": 0, "transactions_skipped": 0}
    admin_email = (admin_email or "").strip().lower() or None

    async with async_session_maker() as session:
        # --- users + workspaces ---------------------------------------------
        user_map: dict[str, User] = {}
        for src in users:
            email = (src.get("email") or "").strip().lower()
            if not email:
                continue
            user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
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

        workspace_map: dict[str, Workspace] = {}
        for src_id, user in user_map.items():
            workspace_map[src_id] = await create_personal_workspace_for_user(session, user)

        # --- buckets -> accounts or categories ------------------------------
        # bucket_id -> ObjectId of the target entity, and (accounts mode) the
        # account a transaction tagged with that bucket should land on.
        bucket_entity: dict[str, Any] = {}
        for bucket in buckets:
            src_id = bucket.get("userId")
            owner = user_map.get(src_id)
            if owner is None:
                continue
            workspace = workspace_map[src_id]
            name = (bucket.get("name") or "Uncategorized").strip()
            if buckets_as == "accounts":
                entity = await _get_or_create_account(session, workspace.id, owner.id, name)
                stats["accounts"] += 1
            else:
                entity = await _get_or_create_category(session, workspace.id, owner.id, bucket)
                stats["categories"] += 1
            bucket_entity[bucket["id"]] = entity

        # categories mode needs one account to hold every transaction.
        imported_account: dict[str, Account] = {}
        if buckets_as == "categories":
            for src_id, owner in user_map.items():
                imported_account[src_id] = await _get_or_create_account(
                    session, workspace_map[src_id].id, owner.id, IMPORTED_ACCOUNT_NAME
                )
                stats["accounts"] += 1

        fallback_account: dict[str, Account] = {}

        def _bucket_account(src: dict[str, Any]) -> Account | None:
            """Account a transaction lands on in accounts mode (its bucket's)."""
            return bucket_entity.get(src.get("bucketId") or "")

        # --- transactions ---------------------------------------------------
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
            category = None
            if buckets_as == "accounts":
                account = _bucket_account(src)
                if account is None:
                    account = fallback_account.get(src["userId"])
                    if account is None:
                        account = await _get_or_create_account(
                            session, workspace.id, owner.id, FALLBACK_ACCOUNT_NAME
                        )
                        fallback_account[src["userId"]] = account
                        stats["accounts"] += 1
            else:
                account = imported_account[src["userId"]]
                category = bucket_entity.get(src.get("bucketId") or "")

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
    parser.add_argument("--buckets-as", choices=("accounts", "categories"), default="accounts",
                        help="Map each bucket to an account (default) or a category")
    parser.add_argument("--admin-email", default=None,
                        help="Email to mark as is_superuser (bootstrap admin)")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    if not args.input.is_dir():
        _log(f"Input directory not found: {args.input}")
        return 2
    return asyncio.run(run(args.input, args.admin_email, args.dry_run, args.buckets_as))


if __name__ == "__main__":
    sys.exit(main())
