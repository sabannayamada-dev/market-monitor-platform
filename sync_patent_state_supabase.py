from __future__ import annotations

import argparse
import json
from pathlib import Path

from patent_monitor.supabase_state import SupabaseStateConfig, SupabaseStateStore


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Supabase Storageと特許監視SQLiteを同期します")
    parser.add_argument("direction", choices=("upload", "download"))
    parser.add_argument(
        "--database",
        default=str(ROOT / "outputs" / "patent_monitor" / "patent_monitor.sqlite3"),
    )
    args = parser.parse_args()
    config = SupabaseStateConfig.from_env()
    if config is None:
        raise SystemExit("SUPABASE_URLとSUPABASE_SECRET_KEYを環境変数に設定してください")
    store = SupabaseStateStore(config)
    database = Path(args.database)
    result = store.backup(database) if args.direction == "upload" else store.restore(database)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
