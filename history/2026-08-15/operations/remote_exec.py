import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "ssh_runtime"))

import paramiko


command = sys.argv[1]
if command == "--file":
    with open(sys.argv[2], encoding="utf-8") as source:
        command = source.read()

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(
    "160.251.252.249",
    username="app",
    key_filename=r"C:\Users\saban\Documents\Codex\2026-08-14\new-chat\work\ssh\patent-news-monitor-key.pem",
    timeout=15,
)
stdin, stdout, stderr = client.exec_command(command, timeout=300)
stdin.close()
sys.stdout.buffer.write(stdout.read())
sys.stderr.buffer.write(stderr.read())
status = stdout.channel.recv_exit_status()
client.close()
raise SystemExit(status)
