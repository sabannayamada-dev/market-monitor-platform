import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "ssh_runtime_temp"))
import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    "160.251.252.249",
    username="app",
    key_filename=r"C:\Users\saban\Documents\Codex\2026-08-14\new-chat\work\ssh\patent-news-monitor-key.pem",
    timeout=15,
)
with client.open_sftp() as sftp:
    sftp.put(sys.argv[1], sys.argv[2])
client.close()
