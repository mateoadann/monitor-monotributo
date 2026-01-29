import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
DEFAULT_UPLOADS = os.path.join(PROJECT_ROOT, "uploads")
DOWNLOADS = os.path.join(PROJECT_ROOT, "downloads")


def iter_files(root: str):
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


def check_empty(path: str, label: str) -> bool:
    if not os.path.exists(path):
        return True
    files = list(iter_files(path))
    if files:
        print(f"{label} tiene archivos:", file=sys.stderr)
        for item in files:
            print(f"- {item}", file=sys.stderr)
        return False
    return True


def main() -> int:
    upload_root = os.environ.get("UPLOAD_FOLDER", DEFAULT_UPLOADS)
    ok = True
    ok = check_empty(upload_root, "uploads") and ok
    ok = check_empty(DOWNLOADS, "downloads") and ok
    if ok:
        print("OK: uploads y downloads vacios.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
