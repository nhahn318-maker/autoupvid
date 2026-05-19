from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig


def get_accounts(config: AppConfig) -> dict[str, dict[str, Any]]:
    accounts = config.data.get("accounts")
    if isinstance(accounts, dict) and accounts:
        return accounts
    return {
        "account1": {
            "label": "Account 1",
            "token_file": config.data["paths"].get("token_file", "token.json"),
            "state_dir": "data/state/accounts/account1",
        }
    }


def get_active_account_id(config: AppConfig) -> str:
    accounts = get_accounts(config)
    active = config.data.get("active_account", "account1")
    return active if active in accounts else next(iter(accounts))


def get_active_account(config: AppConfig) -> tuple[str, dict[str, Any]]:
    account_id = get_active_account_id(config)
    return account_id, get_accounts(config)[account_id]


def account_token_path(config: AppConfig) -> Path:
    _, account = get_active_account(config)
    return config.root / account["token_file"]


def account_state_dir(config: AppConfig) -> Path:
    _, account = get_active_account(config)
    return config.root / account["state_dir"]


def account_state_dirs(config: AppConfig) -> dict[str, Path]:
    return {
        account_id: config.root / account["state_dir"]
        for account_id, account in get_accounts(config).items()
    }
