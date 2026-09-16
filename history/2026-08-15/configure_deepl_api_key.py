from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_HOST = "160.251.252.249"
DEFAULT_USER = "app"
DEFAULT_SSH_KEY = Path(
    r"C:\Users\saban\Downloads\この中の鍵大事だから消すな\patent-news-monitor-key.pem"
)
REMOTE_SECRET = "/var/lib/gdelt-news-monitor/deepl_api_key"


def main() -> int:
    host = os.getenv("MARKET_MONITOR_HOST", DEFAULT_HOST)
    user = os.getenv("MARKET_MONITOR_USER", DEFAULT_USER)
    ssh_key = Path(os.getenv("MARKET_MONITOR_SSH_KEY", str(DEFAULT_SSH_KEY)))
    if not ssh_key.is_file():
        raise SystemExit(f"SSH鍵が見つかりません: {ssh_key}")

    api_key = getpass.getpass("DeepL APIキーを入力（画面には表示されません）: ").strip()
    if not api_key:
        raise SystemExit("空のAPIキーは保存しません。")
    confirmation = getpass.getpass("確認のため、もう一度入力: ").strip()
    if api_key != confirmation:
        raise SystemExit("入力が一致しないため、保存しませんでした。")

    ssh = shutil.which("ssh")
    if not ssh:
        raise SystemExit("Windows OpenSSHのssh.exeが見つかりません。")
    remote_command = (
        "set -eu; umask 077; secret=; IFS= read -r secret; test -n \"$secret\"; "
        f"temporary={REMOTE_SECRET}.tmp; "
        "printf '%s\\n' \"$secret\" > \"$temporary\"; chmod 600 \"$temporary\"; "
        f"mv \"$temporary\" {REMOTE_SECRET}; echo saved"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="deepl-key-setup-") as temporary_dir:
            restricted_key = Path(temporary_dir) / "ssh_key.pem"
            shutil.copyfile(ssh_key, restricted_key)
            username = os.environ.get("USERNAME", "").strip()
            if not username:
                raise RuntimeError("Windowsユーザー名を取得できません。")
            acl = subprocess.run(
                [
                    "icacls.exe",
                    str(restricted_key),
                    "/inheritance:r",
                    "/grant:r",
                    f"{username}:(R)",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
            if acl.returncode != 0:
                raise RuntimeError("一時SSH鍵のアクセス権を制限できませんでした。")
            completed = subprocess.run(
                [
                    ssh,
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes",
                    "-i", str(restricted_key),
                    f"{user}@{host}",
                    remote_command,
                ],
                input=api_key + "\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0 or completed.stdout.strip() != "saved":
                detail = completed.stderr.strip() or f"ssh exit={completed.returncode}"
                raise RuntimeError(f"VPSへの保存に失敗しました: {detail}")
    finally:
        api_key = ""
        confirmation = ""

    print("DeepL APIキーをVPSへ安全に保存しました。値は表示・記録していません。")
    print(f"保存先: {REMOTE_SECRET} (権限600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
