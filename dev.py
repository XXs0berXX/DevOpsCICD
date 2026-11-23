"""Run the whole NestMarket stack locally with one command:  python dev.py

  moto (fake AWS: S3, SQS, DynamoDB, SES)  http://localhost:5000
  API (FastAPI)                            http://localhost:8000/docs
  Frontend (static files, stands in for S3) http://localhost:8080
  Worker                                   prints every email it sends

Press Ctrl+C to stop everything.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
PY = sys.executable

env = os.environ | {
    "AWS_ENDPOINT_URL": "http://localhost:5000",  # boto3 sends every AWS call to moto
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "FRONTEND_URL": "http://localhost:8080",
    "PYTHONUNBUFFERED": "1",
}


def wait_for_port(port: int, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    sys.exit(f"Port {port} did not open in time")


procs = []


def start(name: str, args: list[str], cwd: Path) -> None:
    print(f"starting {name}...")
    procs.append(subprocess.Popen(args, cwd=cwd, env=env))


try:
    start("moto (fake AWS)", [PY, "-m", "moto.server", "-H", "127.0.0.1", "-p", "5000"], ROOT)
    wait_for_port(5000)
    subprocess.run([PY, "infra/local_setup.py"], cwd=ROOT, env=env, check=True)

    start("api", [PY, "-m", "uvicorn", "main:app", "--port", "8000"], ROOT / "api")
    start("worker", [PY, "worker.py"], ROOT / "worker")
    start("frontend", [PY, "-m", "http.server", "8080", "--bind", "127.0.0.1"], ROOT / "frontend")
    wait_for_port(8000)

    print("\n  Frontend:  http://localhost:8080\n  API docs:  http://localhost:8000/docs\n"
          "  Emails are printed below by the worker. Ctrl+C to stop.\n")
    while all(p.poll() is None for p in procs):
        time.sleep(1)
    print("A process exited - shutting down.")
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
